from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import functools
import math
import re

import geopandas as gpd
import pyogrio
from shapely.geometry import Point


LAKE_ID_CANDIDATES = [
    "lake_id_project",
    "lake_id",
    "Lake_ID",
    "LAKE_ID",
    "id",
]

# Allow-listed lake_id shape. LakeCCI IDs observed in this dataset look
# like "CCI_001935"; this is deliberately permissive of letters, digits,
# underscore and hyphen while rejecting quotes, whitespace, and anything
# else that has special meaning inside the OGR SQL `where=` filter built
# in load_lake_by_id(). This is defense in depth alongside the existing
# quote-escaping below -- the escaping alone is fragile if this app is
# ever exposed beyond a single-user Colab session.
LAKE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class LakeNotFoundError(ValueError):
    """Raised when a LakeCCI lake cannot be found."""


@dataclass(frozen=True)
class LakeRecord:
    lake_id: str
    latitude: float
    longitude: float
    geometry: object
    id_column: str
    receptor_method: str


@functools.lru_cache(maxsize=None)
def identify_lake_id_column(
    geopackage_path: str | Path,
) -> str:
    """Identify the LakeCCI ID column, cached per geopackage_path.

    load_lake_by_id() calls this on every single lookup even though the
    GeoPackage's schema is fixed for the lifetime of the session, which
    means a fresh pyogrio.read_info() disk read on every lake load --
    real cost on Drive-mounted storage. Failed lookups (missing file,
    unrecognised schema) are not cached, since lru_cache does not cache
    raised exceptions. If the underlying file is ever replaced mid
    session, call identify_lake_id_column.cache_clear() first.
    """
    geopackage_path = Path(geopackage_path)

    if not geopackage_path.exists():
        raise FileNotFoundError(
            f"GeoPackage not found: {geopackage_path}"
        )

    information = pyogrio.read_info(
        geopackage_path
    )

    fields = list(information["fields"])

    for candidate in LAKE_ID_CANDIDATES:
        if candidate in fields:
            return candidate

    raise ValueError(
        "Could not identify the LakeCCI ID column. "
        f"Available columns: {fields}"
    )


def load_lake_by_id(
    geopackage_path: str | Path,
    lake_id: str,
) -> LakeRecord:
    """Load one LakeCCI polygon and its receptor coordinates."""
    geopackage_path = Path(geopackage_path)
    lake_id = str(lake_id).strip()

    if not lake_id:
        raise ValueError(
            "Lake ID cannot be empty."
        )

    if not LAKE_ID_PATTERN.fullmatch(lake_id):
        raise ValueError(
            f"Lake ID {lake_id!r} contains unexpected characters; "
            "expected only letters, digits, underscores and hyphens."
        )

    id_column = identify_lake_id_column(
        geopackage_path
    )

    escaped_id = lake_id.replace(
        "'",
        "''",
    )

    lake = pyogrio.read_dataframe(
        geopackage_path,
        where=(
            f'"{id_column}" = '
            f"'{escaped_id}'"
        ),
    )

    if lake.empty:
        raise LakeNotFoundError(
            f"Lake {lake_id!r} was not found."
        )

    if lake.crs is None:
        raise ValueError(
            "The LakeCCI dataset has no CRS."
        )

    lake = lake.to_crs("EPSG:4326")
    record = lake.iloc[0]
    geometry = record.geometry

    if geometry is None or geometry.is_empty:
        raise ValueError(
            f"Lake {lake_id!r} has an empty geometry."
        )

    available_columns = set(lake.columns)

    if {
        "centroid_lat",
        "centroid_lon",
    }.issubset(available_columns):
        latitude = float(
            record["centroid_lat"]
        )

        longitude = float(
            record["centroid_lon"]
        )

        receptor_method = "LakeCCI centroid attributes"

        centroid_point = Point(
            longitude,
            latitude,
        )

        # If an attribute centroid lies outside an irregular lake,
        # replace it with a guaranteed interior point.
        if not geometry.covers(centroid_point):
            interior_point = (
                geometry.representative_point()
            )

            latitude = float(interior_point.y)
            longitude = float(interior_point.x)
            receptor_method = (
                "interior representative point"
            )

    else:
        interior_point = (
            geometry.representative_point()
        )

        latitude = float(interior_point.y)
        longitude = float(interior_point.x)
        receptor_method = (
            "interior representative point"
        )

    return LakeRecord(
        lake_id=lake_id,
        latitude=latitude,
        longitude=longitude,
        geometry=geometry,
        id_column=id_column,
        receptor_method=receptor_method,
    )


def load_lakes_in_bounds(
    geopackage_path: str | Path,
    bounds,
    maximum_features: int = 300,
    simplify_tolerance: float = 0.001,
) -> list[dict]:
    """Read only LakeCCI polygons intersecting the visible map bounds."""
    if not bounds or len(bounds) != 2:
        raise ValueError("The current map bounds are unavailable.")
    south, west = (float(value) for value in bounds[0])
    north, east = (float(value) for value in bounds[1])
    if south >= north or west >= east:
        raise ValueError("The current map bounds are invalid.")

    maximum_features = int(maximum_features)
    if maximum_features < 1:
        raise ValueError("maximum_features must be positive.")
    id_column = identify_lake_id_column(geopackage_path)
    lakes = pyogrio.read_dataframe(
        geopackage_path,
        columns=[id_column],
        bbox=(west, south, east, north),
        max_features=maximum_features + 1,
    )
    if len(lakes) > maximum_features:
        raise ValueError(
            f"More than {maximum_features} LakeCCI polygons intersect "
            "this view. Zoom closer and load again."
        )
    if lakes.empty:
        raise LakeNotFoundError(
            "No LakeCCI polygons intersect the current map view."
        )
    if lakes.crs is None:
        raise ValueError("The LakeCCI dataset has no CRS.")

    lakes = lakes.to_crs("EPSG:4326")
    lakes = lakes[
        lakes.geometry.notna() & ~lakes.geometry.is_empty
    ].copy()
    if simplify_tolerance > 0:
        lakes.geometry = lakes.geometry.simplify(
            float(simplify_tolerance),
            preserve_topology=True,
        )

    return sorted(
        [
            {
                "lake_id": str(record[id_column]),
                "geometry_type": record.geometry.geom_type,
                "geometry_geojson": record.geometry.__geo_interface__,
            }
            for _, record in lakes.iterrows()
        ],
        key=lambda item: item["lake_id"],
    )


def find_lake_near_point(
    geopackage_path: str | Path,
    latitude: float,
    longitude: float,
    maximum_distance_km: float = 25.0,
) -> LakeRecord:
    """Return the closest LakeCCI lake within a bounded map-click query."""
    geopackage_path = Path(geopackage_path)
    latitude = float(latitude)
    longitude = float(longitude)
    maximum_distance_km = float(maximum_distance_km)
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("Map-click coordinates are outside valid ranges.")
    if maximum_distance_km <= 0:
        raise ValueError("maximum_distance_km must be positive.")

    id_column = identify_lake_id_column(geopackage_path)
    latitude_delta = maximum_distance_km / 110.574
    longitude_scale = max(abs(math.cos(math.radians(latitude))), 0.10)
    longitude_delta = maximum_distance_km / (111.320 * longitude_scale)
    candidates = pyogrio.read_dataframe(
        geopackage_path,
        columns=[id_column],
        bbox=(
            longitude - longitude_delta,
            latitude - latitude_delta,
            longitude + longitude_delta,
            latitude + latitude_delta,
        ),
    )
    if candidates.empty:
        raise LakeNotFoundError(
            f"No LakeCCI lake was found within {maximum_distance_km:.0f} km "
            "of the clicked location. Zoom closer and click again."
        )
    if candidates.crs is None:
        raise ValueError("The LakeCCI dataset has no CRS.")

    candidates = candidates.to_crs("EPSG:4326")
    click_point = Point(longitude, latitude)
    covering = candidates[candidates.geometry.covers(click_point)]
    if not covering.empty:
        selected = covering.geometry.area.idxmin()
    else:
        metric_crs = candidates.estimate_utm_crs()
        if metric_crs is None:
            metric_crs = "EPSG:3857"
        metric_candidates = candidates.to_crs(metric_crs)
        metric_click = gpd.GeoSeries(
            [click_point],
            crs="EPSG:4326",
        ).to_crs(metric_crs).iloc[0]
        distances = metric_candidates.geometry.distance(metric_click)
        selected = distances.idxmin()
        if float(distances.loc[selected]) > maximum_distance_km * 1000:
            raise LakeNotFoundError(
                f"No LakeCCI lake was found within "
                f"{maximum_distance_km:.0f} km of the clicked location. "
                "Zoom closer and click again."
            )

    return load_lake_by_id(
        geopackage_path=geopackage_path,
        lake_id=str(candidates.loc[selected, id_column]),
    )

