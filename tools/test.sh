#!/usr/bin/env bash
#
# Run the whole test suite. Parallel by default, serial when it cannot be.
#
# WHY THIS EXISTS. The suite takes 247.9 s serially and 44.5 s with `-n auto` —
# the same 3,484 tests either way (measured 2026-10-22). The README has said
# `-n auto` since the parallel work landed, and it still was not what got run: a
# flag you have to remember is a flag that gets dropped. So the default lives in a
# command, not in a paragraph.
#
# WHY NOT `addopts` IN pyproject.toml. That is the obvious place and it fails
# badly: `addopts` is applied unconditionally, so on a machine where the scheduler
# is not installed — a fresh clone, CI before its install step, the AFK sandbox
# before `sandbox-prepare.sh` runs — pytest dies with
#
#     error: unrecognized arguments: -n
#
# and runs zero tests. A default that turns "no scheduler" into "no tests" is worse
# than no default. Verified: the same experiment also showed `PYTEST_ADDOPTS` has
# the identical problem, and that a conftest hook cannot rescue it because conftest
# files are loaded AFTER the ini options are parsed — too late to remove the flag.
#
# WHY THE SCHEDULER IS INSTALLED HERE RATHER THAN DECLARED. Adding it to
# `pyproject.toml` rewrites `uv.lock`, and `deploy/deploy-41.sh` refuses to deploy
# when that lockfile's hash differs from the one on 41. A test-speed change must not
# require a production environment update.
#
# Anything passed to this script is forwarded to pytest, so `./tools/test.sh -k foo`
# and `./tools/test.sh -n 0` both work — the second is how to ask for serial on
# purpose.
set -euo pipefail

cd "$(dirname "$0")/.."

# Default to parallel; an explicit `-n` from the caller wins.
# `-n auto` is the default now (it lives in pyproject's addopts, and the scheduler
# is a declared dev dependency, so a plain `uv sync --dev` provides it). This script
# exists only to make the run self-contained: it ensures the venv matches the
# lockfile, then runs pytest with anything the caller passed.
#
# ⚠️ It no longer installs the scheduler. It used to, because xdist was not declared
# anywhere — and that was the defect: a fresh worktree venv had no xdist, so
# `uv run pytest -n auto` died with `unrecognized arguments: -n` and the only way
# forward was to drop `-n`, silently back to serial (279 s vs 55 s, measured
# 2026-10-23). Declaring it fixed the cause; installing it here would only hide a
# future recurrence.
if ! .venv/bin/python -c 'import xdist' >/dev/null 2>&1; then
  echo "tools/test.sh: the venv is missing the parallel scheduler." >&2
  echo "  Run \`uv sync --locked --dev\` first; this script does not install it," >&2
  echo "  because a missing declared dependency means the venv is out of date." >&2
fi

exec env PYTHONPATH="$PWD/src" .venv/bin/python -m pytest tests -p no:warnings "$@"
