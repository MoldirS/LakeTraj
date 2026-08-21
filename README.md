# LakeTraj Docker release v1.1.0

LakeTraj is a Solara interface for configuring, running and visualizing
HYSPLIT backward trajectories for LakeCCI lake receptors or independent
manual locations.

The local Docker interface is available at:

`http://localhost:8765`

Meteorological files and completed results are stored outside the Docker
container so they remain available after restarts and rebuilds.

## 1. Requirements

Install Docker Desktop for the correct Mac processor from the official
Docker website and make sure the Docker engine is running.

LakeTraj requires a separately obtained HYSPLIT installation.

HYSPLIT software is not distributed with this repository. Obtain and use
HYSPLIT according to the NOAA Air Resources Laboratory HYSPLIT terms and
installation instructions.

## 2. LakeCCI data

The required LakeCCI GeoPackage must be available at:

`data/lakecci_polygons_wgs84.gpkg`

Do not rename the file unless the LakeTraj configuration is updated
accordingly.

## 3. HYSPLIT installation

HYSPLIT must be installed separately from the LakeTraj repository.

The expected installation contains at least:

```text
exec/hyts_std
bdyfiles/ASCDATA.CFG