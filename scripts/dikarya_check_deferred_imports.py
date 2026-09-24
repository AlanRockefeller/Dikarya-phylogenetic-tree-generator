#!/usr/bin/env python
"""Resolve every function-local ``from app.* import name`` without running it.

Why this exists
---------------
``scripts/dikarya-preflight`` imports the modules the web and worker processes
need, which catches a broken import at module scope. It cannot catch a broken
import inside a function body, because that line does not execute until the
code path runs -- and Python compiles it happily either way.

That is not hypothetical. On 2026-09-09 ``app/workers/tasks.py`` was deployed
with, at what is now line ~1180:

    from app.services.mycomap_service import (
        ...
        describe_mycomap_queue_wait,     # never existed; the real name is
        ...                              # describe_mycomap_queue_position
    )

It sits inside ``run_phylo_job`` under ``if prepared["status"] ==
"waiting_for_ncbi"``. Every module still imported cleanly, so the preflight
passed and the restart looked healthy. Job a7793b5b (a user's iNat -> MycoMap
tree) deferred through five MycoMap poll cycles over four minutes and then died
at step=input with an ImportError. This is the same failure shape as the
2026-08-14 incident, one scope deeper.

Deferred imports are not a smell to be refactored away here -- there are ~600 of
them and they exist to break circular imports and keep worker startup cheap. So
check them statically instead: parse each module, find the ImportFrom nodes that
live inside a function, import the named module, and assert the attribute is
really there.

Limits, stated honestly:
  * Only ``app.*`` targets are checked. Third-party APIs are the vendor's
    problem and would make this slow.
  * A ``from app.x import y`` where ``y`` is a submodule rather than an
    attribute is resolved with importlib before being reported, because a
    submodule is not an attribute of its package until something imports it.
  * ``import *`` is skipped -- there is no name to verify.
  * This proves the name exists, not that calling it works.

Exit 0 when every deferred name resolves, 1 otherwise.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import sys


def _local_import_froms(tree: ast.AST):
    """Yield ImportFrom nodes that are nested inside a function body."""
    out: list[ast.ImportFrom] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.depth = 0

        def visit_FunctionDef(self, node):  # noqa: N802
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ImportFrom(self, node):  # noqa: N802
            if self.depth and node.module and node.module.startswith("app"):
                out.append(node)
            self.generic_visit(node)

    Visitor().visit(tree)
    return out


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))

    failures: list[str] = []
    checked = 0

    for path in sorted((root / "app").rglob("*.py")):
        rel = path.relative_to(root)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        except SyntaxError as exc:
            failures.append(f"{rel}:{exc.lineno}: syntax error: {exc.msg}")
            continue

        for node in _local_import_froms(tree):
            try:
                module = importlib.import_module(node.module)
            except Exception as exc:
                failures.append(
                    f"{rel}:{node.lineno}: cannot import module "
                    f"{node.module!r}: {type(exc).__name__}: {exc}"
                )
                continue

            for alias in node.names:
                if alias.name == "*":
                    continue
                checked += 1
                if hasattr(module, alias.name):
                    continue
                # Not an attribute yet -- it may be an unimported submodule.
                try:
                    importlib.import_module(f"{node.module}.{alias.name}")
                except Exception:
                    failures.append(
                        f"{rel}:{node.lineno}: cannot import name "
                        f"{alias.name!r} from {node.module!r}"
                    )

    if failures:
        sys.stderr.write(
            "deferred-import check: FAILED -- these names do not exist. Each one\n"
            "is a runtime ImportError waiting for the code path that reaches it:\n\n"
        )
        for line in failures:
            sys.stderr.write(f"  {line}\n")
        sys.stderr.write(f"\n{len(failures)} broken, {checked} names checked\n")
        return 1

    print(f"deferred-import check: OK ({checked} function-local app.* names resolve)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
