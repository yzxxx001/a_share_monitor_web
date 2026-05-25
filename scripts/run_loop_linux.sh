#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
stock-monitor loop --config config/config.yaml --run-now
