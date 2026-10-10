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

