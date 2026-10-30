#!/usr/bin/env python3
"""Answer one question per test: does anything go red when the code under it breaks?

This is the audit `TEST-DISCIPLINE.md` §4.3 asks for — "a test that supposedly pins
a defect must be verifiable in reverse: put the wrong implementation back and it
turns red". A test that survives every mutation of the code it claims to cover is
not pinning anything; it is a change-probe that happens to run code.

WHY NOT `mutmut`. Tried it. Measured: it generated **43,759** mutants for this
repository and ignored the config file twice in two different spellings — both
times regenerating the whole tree. At 45.6 s per mutant (a full parallel run) the
whole-repo answer costs ~550 hours, and the tool's own filtering was not something
I could get to work in two attempts. A narrower tool that I can read is worth more
here than a general one I cannot configure.

WHAT IT DOES. For a given source file:

1. Find every comparison and boolean operator — the sites where "change the
   implementation back" has a mechanical meaning.
2. For each, write a mutated copy of the file (operator inverted / condition
   negated), run the tests, and record whether they went red.
3. Restore the original and move on. A mutant that survives (**all tests still
   green**) is reported: that code is not pinned by anything.

COST. One full test run per mutant. Measured on this repository: 45.6 s parallel,
249 s serial. Use `--limit` and `--file` to keep a run bounded; the point is to
sample the most dangerous modules, not to mutate 49k lines.

⚠️ `--tests` MUST be the whole suite. Measured the hard way: narrowing it to one
file reported a SURVIVED mutant at `gateway_config.py:341` (`not in _FALSY_SETTINGS`
inverted), and the full suite kills that mutant with **8 failing tests**. A narrow
run reports every line as unpinned simply because the test that pins it lives in
another file — the audit would produce a list of deletions, each one wrong.

Run:
    tools/mutation-audit.py --file src/aiops_diagnostics/gateway/gateway_config.py --limit 20
    tools/mutation-audit.py --file ... --tests "tests/test_gateway_api.py"
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[1]

#: Each invertible comparison operator and the source spelling of both directions.
#: `is` / `is not` are left out: inverting them usually produces a type error
#: rather than a plausible wrong implementation, and a mutant that fails to import
#: says nothing about whether a test would have caught the real defect.
SPELLING: dict[str, tuple[str, str]] = {
    "LtE": ("<=", ">"),
    "Gt": (">", "<="),
    "Lt": ("<", ">="),
    "GtE": (">=", "<"),
    "Eq": ("==", "!="),
    "NotEq": ("!=", "=="),
    "In": (" in ", " not in "),
    "NotIn": (" not in ", " in "),
}


def sites(source: str, tree: ast.Module) -> list[tuple[int, int, str]]:
    """(line, column, operator-spelling) for each invertible comparison.

    Positions come from the SOURCE TEXT inside the comparison's own span, not from
    the AST: operator nodes carry no position (verified — `Compare.ops[0].lineno`
    is None for every node), and a comparison may span lines, so the comparison's
    line/column points at its first operand rather than at the operator. Searching
    the span for the spelling is the only way to find the text to replace.

    A comparison whose operator spelling does not appear in its own span (a
    formatter oddity, an operator split across tokens) is skipped rather than
    guessed at.
    """
    lines = source.split("\n")
    found: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or not node.ops:
            continue
        for op in node.ops:
            kind = type(op).__name__
            if kind not in SPELLING:
                continue
            spelling = SPELLING[kind][0]
            span_start = (node.lineno, node.col_offset)
            span_end = (node.end_lineno or node.lineno, node.end_col_offset or node.col_offset)
            for lineno in range(span_start[0], span_end[0] + 1):
                segment = lines[lineno - 1]
                begin = span_start[1] if lineno == span_start[0] else 0
                finish = span_end[1] if lineno == span_end[0] else len(segment)
                idx = segment.find(spelling, begin, finish)
                if idx != -1:
                    found.append((lineno, idx, spelling))
                    break
    return sorted(set(found))


def run_tests(target: str, timeout: int) -> bool:
    """True when the suite stayed green (the mutant survived)."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            target,
            "-q",
            "-p",
            "no:warnings",
            "-x",
            "--no-header",
            "-n",
            "auto",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={
            "PYTHONPATH": str(REPO / "src"),
            "PATH": "/usr/local/bin:/usr/bin:/bin:" + str(pathlib.Path.home() / ".local/bin"),
        },
    )
    return completed.returncode == 0


def mutate(source: str, line: int, column: int, spelling: str) -> str | None:
    """Invert the operator at exactly ``(line, column)``; None when not there."""
    lines = source.split("\n")
    if not 1 <= line <= len(lines):
        return None
    if lines[line - 1][column : column + len(spelling)] != spelling:
        return None
    inverse = next((inv for old, inv in SPELLING.values() if old == spelling), None)
    if inverse is None:
        return None
    segment = lines[line - 1]
    lines[line - 1] = segment[:column] + inverse + segment[column + len(spelling) :]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="source file to mutate")
    parser.add_argument(
        "--tests",
        default="tests",
        help="what to run per mutant; MUST be the whole suite — see the warning below",
    )
    parser.add_argument("--limit", type=int, default=20, help="how many mutants to try")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    path = (REPO / args.file).resolve()
    original = path.read_text(encoding="utf-8")
    found = sites(original, ast.parse(original))[: args.limit]
    total_sites = len(sites(original, ast.parse(original)))
    print(f"{path.relative_to(REPO)}: {len(found)} mutant(s) to try (of {total_sites})", flush=True)

    survived = []
    started = time.time()
    try:
        for number, (line, column, spelling) in enumerate(found, 1):
            mutated = mutate(original, line, column, spelling)
            if mutated is None:
                print(f"  [{number}/{len(found)}] line {line}: could not splice; skipped")
                continue
            path.write_text(mutated, encoding="utf-8")
            t0 = time.time()
            try:
                green = run_tests(args.tests, args.timeout)
            except subprocess.TimeoutExpired:
                print(f"  [{number}/{len(found)}] line {line}: timed out (counted as killed)")
                continue
            finally:
                path.write_text(original, encoding="utf-8")
            mark = "SURVIVED" if green else "killed"
            print(f"  [{number}/{len(found)}] line {line}: {mark}  ({time.time() - t0:.0f}s)", flush=True)
            if green:
                survived.append(line)
    finally:
        path.write_text(original, encoding="utf-8")

    print()
    print(f"tried {len(found)} mutant(s) in {(time.time() - started) / 60:.1f} min")
    if survived:
        print(f"SURVIVED at lines: {survived}")
        print("Those lines are not pinned by any test — change them and nothing goes red.")
        return 1
    print("every mutant was killed: the code under test is pinned.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
