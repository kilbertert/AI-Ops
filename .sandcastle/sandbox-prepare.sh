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

# The test scheduler, installed into the venv rather than declared in the lockfile.
# `implement.md` runs the suite with `pytest -n auto` (afk 1.7.28): measured on
# AI-Ops, 249.7 s serially vs 46.4 s parallel for the same tests, and the agent
# runs that check on EVERY lap. Not a dev dependency: that rewrites uv.lock, and
# this repository's CD refuses to deploy on a lockfile hash mismatch.
#
# ⚠️ Every run, not once — `uv sync` above removes anything the lockfile does not
# name. Same versions as CI (.github/workflows/ci.yml).
uv pip install "pytest-xdist==3.8.0" "execnet==2.1.2"

