#!/usr/bin/env python3
"""Compare the outputs of two IQ-TREE runs made with the same input, options and seed.

usage: compare-iqtree-runs.py PREFIX_A PREFIX_B

Trees, consensus trees, split files, distance files and UFBoot trees must be
byte-identical. The .iqtree report, .log and .model.gz are compared after
dropping the lines that legitimately differ between builds and runs (version
banner, build date, command line, host, wall/CPU times, timestamps).
Exit status 0 if everything matches, 1 otherwise.
"""
import gzip
import re
import sys
from pathlib import Path

BYTE_EXACT = ('treefile', 'contree', 'splits.nex', 'mldist', 'bionj', 'ufboot', 'uniqueseq.phy')
NORMALIZED = ('iqtree', 'log', 'model.gz')
DROP = re.compile(r'^(IQ-TREE |IQ-TREE version|IQ-TREE multicore version|Command:|Host:|Time:|'
                  r'Kernel:|Date and Time:|Date and time:| start_real_time:| version:|Seed:|'
                  r'Wrote distance file)')
SUBS = [(re.compile(r'-?\d+h:-?\d+m:-?\d+s'), 'HMS'),   # includes negative "time left" estimates
        (re.compile(r'[\d.]+(e-?\d+)?( ?)(sec|secs|second|seconds)\b'), r'T\2\3'),
        (re.compile(r' using [\d.]+% CPU'), ''),
        (re.compile(r'(CPU time|wall-clock time|Wall-clock time|time used)( for [A-Za-z ]+)?:\s*[\d.]+'),
         r'\1\2: T')]


def text(path):
    if path.suffix == '.gz':
        return gzip.open(path, 'rt', errors='replace').read()
    return path.read_text(errors='replace')


def normalize(txt, prefix):
    out = []
    txt = txt.replace(str(prefix.resolve()), 'PREFIX').replace(str(prefix), 'PREFIX')
    txt = txt.replace(prefix.name, 'PREFIX')
    for line in txt.splitlines():
        if DROP.search(line):
            continue
        for rx, rep in SUBS:
            line = rx.sub(rep, line)
        out.append(line)
    return out


def main():
    a, b = Path(sys.argv[1]), Path(sys.argv[2])
    ok = True
    compared = 0
    for ext in BYTE_EXACT + NORMALIZED:
        pa, pb = Path(f'{a}.{ext}'), Path(f'{b}.{ext}')
        if not pa.exists() and not pb.exists():
            continue
        if pa.exists() != pb.exists():
            print(f'  {ext}: only one run wrote it')
            ok = False
            continue
        compared += 1
        if ext in BYTE_EXACT:
            same = pa.read_bytes() == pb.read_bytes()
        else:
            same = normalize(text(pa), a) == normalize(text(pb), b)
        if not same:
            print(f'  {ext}: differs')
            ok = False
    if compared == 0:
        print('  no output files found')
        ok = False
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
