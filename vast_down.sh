#!/usr/bin/env bash
# Destroy the instance recorded in .last_instance (or pass an id explicitly).
set -euo pipefail
INST_ID="${1:-$(cat .last_instance 2>/dev/null || true)}"
[ -n "${INST_ID}" ] || { echo "no instance id (pass arg or have .last_instance)"; exit 2; }
echo "==> destroying ${INST_ID}"
echo "y" | vastai destroy instance "${INST_ID}"
rm -f .last_instance
