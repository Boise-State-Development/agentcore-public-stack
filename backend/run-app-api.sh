#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/src/apis/app_api"
exec uv run python main.py
