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
n_flag=(-n auto)
for arg in "$@"; do
  case "$arg" in
    -n|-n?*|--numprocesses|--numprocesses=*) n_flag=() ;;
  esac
done

# Provision the scheduler when pytest would otherwise reject the arguments. Note
# that `-n 0` counts: `-n` is a flag xdist REGISTERS, so asking for zero workers
# still fails with `unrecognized arguments: -n` when the plugin is absent (measured).
# Gating on "would this run use more than one worker" was therefore wrong, and it
# made the documented serial escape hatch the one path that could not run.
#
# ⚠️ A failed install is NOT fatal. Review measured the first version: with
# `uv pip install` failing (offline), `set -e` killed the script before a single
# test ran. An offline machine still deserves a test run, and serial is the correct
# degradation — the same rule the pyproject `addopts` idea got wrong, where "no
# scheduler" became "no tests".
needs_scheduler=0
if [ "${#n_flag[@]}" -gt 0 ]; then
  needs_scheduler=1
else
  for arg in "$@"; do
    case "$arg" in -n|-n?*|--numprocesses|--numprocesses=*) needs_scheduler=1 ;; esac
  done
fi
if [ "$needs_scheduler" = "1" ] && ! .venv/bin/python -c 'import xdist' >/dev/null 2>&1; then
  if ! uv pip install --quiet "pytest-xdist==3.8.0" "execnet==2.1.2"; then
    echo "tools/test.sh: could not install pytest-xdist; running serially" >&2
    # Drop a caller-supplied -n too: without the plugin pytest cannot parse it.
    cleaned=()
    skip_next=0
    for arg in "$@"; do
      if [ "$skip_next" = "1" ]; then skip_next=0; continue; fi
      case "$arg" in
        -n) skip_next=1 ;;
        -n?*|--numprocesses|--numprocesses=*) ;;
        *) cleaned+=("$arg") ;;
      esac
    done
    set -- "${cleaned[@]}"
    n_flag=()
  fi
fi

exec env PYTHONPATH="$PWD/src" .venv/bin/python -m pytest tests -p no:warnings "${n_flag[@]}" "$@"
