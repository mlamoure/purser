#!/usr/bin/env bash
set -euo pipefail
reports=${1:-security-reports}
mkdir -p "$reports"
uv export --frozen --no-dev --no-emit-project --format requirements-txt \
  --output-file "$reports/requirements.txt"
uvx pip-audit==2.10.1 --no-deps --disable-pip \
  --requirement "$reports/requirements.txt" --format json --output "$reports/python.json"
