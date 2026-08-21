#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

LAKE_DATA="data/lakecci_polygons_wgs84.gpkg"

# HYSPLIT is intentionally not distributed with LakeTraj.
# Point this to your separately installed/authorized HYSPLIT directory.
HYSPLIT_HOST_DIR="${HYSPLIT_HOST_DIR:-$HOME/hysplit_laketraj/hysplit.v5.4.2_UbuntuOS20.04.6LTS_public}"

if ! command -v docker >/dev/null 2>&1; then
    echo "Docker was not found. Install and start Docker Desktop first."
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "Docker Desktop is installed but is not running. Start it and try again."
    exit 1
fi

if [ ! -f "$LAKE_DATA" ]; then
    echo "Missing LakeCCI GeoPackage: $LAKE_DATA"
    exit 1
fi

if [ ! -x "$HYSPLIT_HOST_DIR/exec/hyts_std" ]; then
    echo "HYSPLIT executable was not found:"
    echo "$HYSPLIT_HOST_DIR/exec/hyts_std"
    echo
    echo "Install HYSPLIT separately and set HYSPLIT_HOST_DIR if needed."
    exit 1
fi

if [ ! -f "$HYSPLIT_HOST_DIR/bdyfiles/ASCDATA.CFG" ]; then
    echo "HYSPLIT ASCDATA.CFG was not found:"
    echo "$HYSPLIT_HOST_DIR/bdyfiles/ASCDATA.CFG"
    exit 1
fi

mkdir -p runtime presentation_results

export HYSPLIT_HOST_DIR

docker compose up --build -d

echo "Waiting for LakeTraj to start..."

for attempt in $(seq 1 60); do
    if curl --silent --fail http://localhost:8765/ >/dev/null; then
        echo "LakeTraj is ready: http://localhost:8765"
        open http://localhost:8765
        exit 0
    fi
    sleep 2
done

echo "LakeTraj did not become ready. Showing container logs:"
docker compose logs --tail=100
exit 1