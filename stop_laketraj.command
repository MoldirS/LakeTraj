#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
docker compose down
echo "LakeTraj stopped. Cached meteorology and results were preserved."

