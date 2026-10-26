#!/usr/bin/env bash
# Repro de carrera al borrar roles em_prod_tests huérfanos.
# Dos pytest/procesos concurrentes no deben fallar ni dejar restos.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "${ROOT}/.venv/bin/python" ]]; then
    PYTHON="${ROOT}/.venv/bin/python"
  else
    PYTHON=python3
  fi
fi
NS="${NS:-1 1 1 5 5 40}"
for N in $NS; do
  echo -n "N=$N: "
  "$PYTHON" scripts/race_drop.py "$N"
done
