#!/usr/bin/env bash
#
# Prepare an AI-Ops sandbox for an AFK run.
#
# A run's workspace starts empty; CI installs the venv the same way. Idempotent —
# sandcastle runs this once per iteration.
#
# Deliberately NOT here: anything touching AIOPS_DATA_HOME. The verify skills
# build their own throwaway data root, and a prepare step that seeded the real
# one would make every run write to production state.
set -euo pipefail

uv sync --locked --dev

# No ad-hoc test-scheduler install here any more. `pytest-xdist` is a declared dev
# dependency (2026-10-23), so the `uv sync` above provides it. It used to live only
# in this hook — which meant any venv that was NOT built by this hook (a fresh
# worktree, a contributor's first checkout) had no scheduler, and `-n auto` failed
# with `unrecognized arguments: -n`. Declaring it is what makes "parallel by
# default" a property of the environment instead of a property of this script.

