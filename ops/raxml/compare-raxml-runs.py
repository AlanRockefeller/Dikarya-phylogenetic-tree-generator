#!/usr/bin/env python3
"""Compare two RAxML-NG runs byte for byte.

    compare-raxml-runs.py <prefixA> <prefixB>

Every result file either run wrote (<prefix>.raxml.*) must exist in both and be
identical, except the log, which is compared without the lines that differ
between any two runs (timings, dates, the version banner, the command line and
file paths), and JSON files (MOOSE), which are compared without their run
metadata (timings, invocation, release). Exits 0 when the runs are identical,
1 otherwise.
"""
import json
import re
import sys
from pathlib import Path

SKIP_SUFFIXES = {".log", ".rba"}  # the log is compared below; .rba is a cache of the input
VOLATILE = re.compile(
    r"^\[\d\d:\d\d:\d\d\]|Elapsed time|Analysis started|RAxML-NG v\. |RAxML-NG was called|"
    r"released on|(^|[\s:])/\w|time:|^\s*$"
)


def results(prefix: Path) -> dict:
    return {
        p.name[len(prefix.name):]: p
        for p in prefix.parent.glob(prefix.name + ".raxml.*")
        if p.suffix not in SKIP_SUFFIXES
    }


# run metadata that differs between any two runs (or names the build)
JSON_VOLATILE = {"elapsed_time", "elapsed_time_total", "invocation", "invocation_time", "release"}


def same_content(pa: Path, pb: Path) -> bool:
    da, db = pa.read_bytes(), pb.read_bytes()
    if da == db:
        return True
    if pa.suffix != ".json":
        return False
    ja, jb = json.loads(da), json.loads(db)
    for j in (ja, jb):
        meta = j.get("metadata") if isinstance(j, dict) else None
        if isinstance(meta, dict):
            for key in JSON_VOLATILE:
                meta.pop(key, None)
    # compared as text, so -0.0 and 0.0 still differ
    return json.dumps(ja, sort_keys=True) == json.dumps(jb, sort_keys=True)


WORKER = re.compile(r"\[worker #(\d+)\]")
MOOSE_MODEL = re.compile(r"Evaluated model \(\s*\d+/\d+\)")


def log_lines(prefix: Path) -> list:
    path = Path(str(prefix) + ".raxml.log")
    if not path.is_file():
        return None
    out, workers, moose_models = [], {}, []
    for line in path.read_text(errors="replace").splitlines():
        # "[hh:mm:ss] text" and "[hh:mm:ss logLH] text": drop the clock, keep the logLH
        line = re.sub(r"^\[\d\d:\d\d:\d\d(?: ([^\]]*))?\] ", lambda m: (m.group(1) or "") + " ", line)
        if VOLATILE.search(line):
            continue
        if MOOSE_MODEL.search(line):
            # MOOSE evaluates models concurrently at --threads 2 and numbers
            # them in completion order; compare the set of results.
            moose_models.append(MOOSE_MODEL.sub("Evaluated model", line))
            continue
        worker = WORKER.search(line)
        if worker:
            # At --threads 2 the coarse-grained workers log concurrently, so
            # their lines interleave differently from run to run even in stock;
            # compare each worker's lines in its own order.
            workers.setdefault(int(worker.group(1)), []).append(line)
        else:
            out.append(line)
    for worker_id in sorted(workers):
        out.extend(workers[worker_id])
    return out + sorted(moose_models)


def main() -> int:
    a, b = Path(sys.argv[1]), Path(sys.argv[2])
    ra, rb = results(a), results(b)
    ok = True
    if not ra:
        print(f"no results for {a}")
        return 1
    for name in sorted(set(ra) | set(rb)):
        if name not in ra or name not in rb:
            print(f"only in one run: {name}")
            ok = False
        elif not same_content(ra[name], rb[name]):
            print(f"differs: {name}")
            ok = False
    la, lb = log_lines(a), log_lines(b)
    if la is None or lb is None or la != lb:
        print("differs: log")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
