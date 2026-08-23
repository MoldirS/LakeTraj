# LakeTraj v1.1.11

LakeTraj is a Solara-based interface for configuring, running, visualizing,
and downloading HYSPLIT backward trajectories for LakeCCI lake receptors
or independent manual locations.

Public deployment:

`https://laketraj.onrender.com`

Local Docker interface:

`http://localhost:8765`

## 1. Main features

LakeTraj supports:

- receptor selection from the LakeCCI lake dataset;
- manual receptor coordinates;
- configurable arrival date, arrival hours, heights, backward duration,
  vertical-motion option, and model-top height;
- GDAS1 meteorology;
- optional GFS 0.25° meteorology;
- local HYSPLIT backward-trajectory execution;
- trajectory visualization on an interactive map;
- filtering by arrival time and arrival height;
- export of trajectory results as ZIP, CSV, GeoJSON, and GeoPackage files.

## 2. HYSPLIT licensing and installation

HYSPLIT is **not distributed with this repository or Docker image**.

LakeTraj requires a separately obtained HYSPLIT installation. Obtain and
use HYSPLIT according to the NOAA Air Resources Laboratory HYSPLIT terms
and installation instructions.

The installation used by LakeTraj must contain at least:

```text
exec/hyts_std
bdyfiles/ASCDATA.CFG
```

The application locates HYSPLIT through the `HYSPLIT_HOME` environment
variable.

For the Render deployment, the separately installed HYSPLIT directory is
stored on persistent storage and exposed to the application as:

```text
HYSPLIT_HOME=/var/data/hysplit
```

The application must therefore be able to access:

```text
/var/data/hysplit/exec/hyts_std
/var/data/hysplit/bdyfiles/ASCDATA.CFG
```

## 3. LakeCCI data

The LakeCCI GeoPackage used by the application is:

```text
data/lakecci_polygons_wgs84.gpkg
```

Inside the Docker image, the default deployment path is:

```text
/app/data/lakecci_polygons_wgs84.gpkg
```

Do not rename the GeoPackage unless the corresponding LakeTraj
configuration is updated.

## 4. Meteorology cache

Meteorological files are stored in runtime storage rather than inside the
Docker image.

Default deployment directories are:

```text
/var/data/runtime/meteorology/gdas1
/var/data/runtime/meteorology/gfs0p25
```

LakeTraj uses a rolling meteorology cache. Files that are already available
for the requested trajectory period are reused instead of being downloaded
again.

For example, if a user calculates trajectories for several different lakes
using the same or overlapping dates, LakeTraj can reuse the previously
downloaded meteorological files.

When the requested meteorology period changes, the application retains
files required by the current plan, downloads missing files, and can remove
obsolete cache files that are no longer required.

This behavior reduces repeated NOAA downloads and makes consecutive
trajectory calculations faster.

## 5. Temporary trajectory results

Completed trajectory packages are written to runtime and persistent result
storage so that they remain available for visualization and download after
the calculation completes.

The Render deployment uses:

```text
LAKETRAJ_RUNTIME_DIR=/var/data/runtime
LAKETRAJ_RESULTS_DIR=/var/data/results
```

Result packages are temporary. The default retention period is:

```text
RESULT_RETENTION_HOURS=3
```

After the retention period expires, completed trajectory result packages
can be removed automatically.

This retention policy applies only to generated trajectory results. It does
**not** remove:

- the HYSPLIT installation;
- the LakeCCI dataset;
- meteorology files that are being managed by the rolling cache.

## 6. Environment variables

The main deployment environment variables are:

```text
HYSPLIT_HOME=/var/data/hysplit
LAKETRAJ_DATA_DIR=/app/data
LAKETRAJ_RUNTIME_DIR=/var/data/runtime
LAKETRAJ_RESULTS_DIR=/var/data/results
RESULT_RETENTION_HOURS=3
```

For Render, `PORT` is supplied automatically by the platform and should not
normally be set manually.

## 7. Local Docker setup

Requirements:

- Docker Desktop;
- the LakeCCI GeoPackage;
- a separately obtained HYSPLIT installation.

A typical local HYSPLIT installation can be exposed to Docker through the
`HYSPLIT_HOST_DIR` environment variable.

Example:

```bash
export HYSPLIT_HOST_DIR="$HOME/hysplit_laketraj/hysplit.v5.4.2_UbuntuOS20.04.6LTS_public"
docker compose up --build -d
```

Then open:

```text
http://localhost:8765
```

To stop LakeTraj:

```bash
docker compose down
```

## 8. Render deployment

LakeTraj is deployed as a Docker web service.

Recommended Render configuration:

```text
Language: Docker
Branch: main
Docker Build Context Directory: .
Dockerfile Path: ./Dockerfile
Health Check Path: /
```

Persistent storage is mounted at:

```text
/var/data
```

The HYSPLIT installation is transferred separately to:

```text
/var/data/hysplit
```

It is intentionally not committed to GitHub and not copied into the Docker
image.

## 9. Repository structure

A simplified project layout is:

```text
LakeTraj/
├── app.py
├── Dockerfile
├── compose.yaml
├── requirements.txt
├── README.md
├── data/
│   └── lakecci_polygons_wgs84.gpkg
└── laketraj/
    ├── hysplit_runner.py
    ├── meteorology.py
    ├── lakes.py
    ├── map_view.py
    └── parser.py
```

## 10. Notes

LakeTraj is intended for backward-trajectory analysis and research
workflows. Meteorological data availability, network performance, and
HYSPLIT execution time depend on the selected dates, trajectory duration,
meteorological dataset, and deployment resources.

**NOAA service dependency:** LakeTraj relies on NOAA-hosted meteorological
archives for GDAS1 and GFS 0.25° data. If the NOAA archive is temporarily
unavailable, slow, or a requested file cannot be accessed, LakeTraj may be
unable to download the required meteorological data. In that case,
HYSPLIT trajectories cannot be calculated until the required files become
available. Previously downloaded meteorological files that are still in
the rolling cache can continue to be reused.

GFS 0.25° files are substantially larger than GDAS1 files, so storage and
download requirements should be considered before running long-duration
GFS trajectories.
