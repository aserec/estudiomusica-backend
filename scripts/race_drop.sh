#!/usr/bin/env bash
# Repro de carrera al borrar roles em_prod_tests huérfanos.
# Dos pytest/procesos concurrentes no deben fallar ni dejar restos.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
NS="${NS:-1 1 1 5 5 40}"
for N in $NS; do
  echo -n "N=$N: "
  python scripts/race_drop.py "$N"
done
