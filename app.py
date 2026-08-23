from pathlib import Path
import os
import shutil
import threading
import time
import zipfile

import geopandas as gpd
import pandas as pd
import solara
from shapely.geometry import LineString

from laketraj.lakes import load_lake_by_id, load_lakes_in_bounds
from laketraj.map_view import (
    EmptyReceptorMapElement,
    LakeSelectionMapElement,
    ManualReceptorMapElement,
    ReceptorMapElement,
    trajectory_filter_options,
)

import datetime as dt
import solara.lab

from laketraj.meteorology import (
    required_gdas1_files,
    required_gfs0p25_files,
    inspect_gdas1_files,
    download_required_gdas1_files,
    inspect_gfs0p25_files,
    gfs0p25_storage_status,
    download_required_gfs0p25_files,
    prune_gfs0p25_cache,
)

from laketraj.hysplit_runner import (
    validate_hysplit_environment,
    run_trajectory_batch,
    save_batch_results,
)

APP_TITLE = "LakeTraj"
APP_VERSION = "1.1.11"

# ---------------------------------------------------------------------
# Deployment-configurable paths
# ---------------------------------------------------------------------

APP_DATA_DIR = Path(
    os.environ.get(
        "LAKETRAJ_DATA_DIR",
        "/content/drive/MyDrive/Fire_Lake_AOD_Project/data_processed/lakes",
    )
)

LAKE_GPKG = APP_DATA_DIR / "lakecci_polygons_wgs84.gpkg"


RUNTIME_ROOT = Path(
    os.environ.get(
        "LAKETRAJ_RUNTIME_DIR",
        "/content/LakeTraj_runtime",
    )
)

PERSISTENT_RESULTS_ROOT = Path(
    os.environ.get(
        "LAKETRAJ_RESULTS_DIR",
        "/content/drive/MyDrive/LakeTraj_results",
    )
)


# Final trajectory packages remain downloadable for this many hours.
# Meteorology is deliberately NOT governed by this retention setting.
RESULT_RETENTION_HOURS = float(
    os.environ.get("RESULT_RETENTION_HOURS", "3")
)
if RESULT_RETENTION_HOURS <= 0:
    raise ValueError("RESULT_RETENTION_HOURS must be greater than zero.")

RESULT_RETENTION_SECONDS = RESULT_RETENTION_HOURS * 60 * 60

VERTICAL_MOTION_OPTIONS = {
    "Model vertical velocity": 0,
    "Isobaric": 1,
    "Isentropic": 2,
}

METEOROLOGY_DATASET_OPTIONS = {
    "GDAS1 (1 degree, recommended default)": "GDAS1",
    "GFS025 (0.25 degree optional; large daily files)": (
        "GFS0P25"
    ),
}

# ---------------------------------------------------------------------
# Interface state
# ---------------------------------------------------------------------

selection_mode = solara.reactive(
    "LakeCCI lake"
)

lake_id_input = solara.reactive(
    "CCI_001935"
)

manual_name_input = solara.reactive(
    "Manual receptor"
)

latitude_input = solara.reactive(
    53.604167
)

longitude_input = solara.reactive(
    108.120834
)
manual_marker_location = solara.reactive(
    (53.604167, 108.120834)
)
manual_map_editing = solara.reactive(True)


# ---------------------------------------------------------------------
# Applied receptor state
# ---------------------------------------------------------------------

applied_receptor = solara.reactive(None)
status_message = solara.reactive(None)
error_message = solara.reactive(None)
map_lake_candidate = solara.reactive(None)
map_selection_message = solara.reactive("")
map_selection_error = solara.reactive("")
map_selection_center = solara.reactive((50.0, 10.0))
map_selection_zoom = solara.reactive(4)
map_selection_bounds = solara.reactive(((35.0, -10.0), (65.0, 30.0)))
map_view_lakes = solara.reactive([])
map_selected_lake_id = solara.reactive(None)
map_lakes_loading = solara.reactive(False)
map_view_lakes_outdated = solara.reactive(False)
map_loaded_bounds_signature = solara.reactive(None)
map_selection_active = solara.reactive(True)

arrival_date = solara.reactive(dt.date(2021, 8, 8))
vertical_motion = solara.reactive("Model vertical velocity")
meteorology_dataset = solara.reactive(
    "GDAS1 (1 degree, recommended default)"
)

model_top_metres = solara.reactive(10000)
selected_arrival_hours = solara.reactive([0, 6, 12, 18])
backward_duration = solara.reactive(120)
selected_heights = solara.reactive([500, 1000, 1500])

applied_trajectory_settings = solara.reactive(None)
trajectory_settings_message = solara.reactive("")
trajectory_settings_error = solara.reactive("")

gdas1_download_message = solara.reactive("")
gdas1_download_error = solara.reactive("")
gdas1_download_refresh = solara.reactive(0)
gdas1_download_in_progress = solara.reactive(False)
gdas1_download_summary = solara.reactive(None)
gfs0p25_download_message = solara.reactive("")
gfs0p25_download_error = solara.reactive("")
gfs0p25_download_in_progress = solara.reactive(False)
gfs0p25_download_summary = solara.reactive(None)
gfs0p25_download_refresh = solara.reactive(0)

# Prevent duplicate meteorology workers from being started accidentally.
gdas1_download_lock = threading.Lock()
gfs0p25_download_lock = threading.Lock()

hysplit_run_message = solara.reactive("")
hysplit_run_error = solara.reactive("")
hysplit_run_summary = solara.reactive(None)
hysplit_run_in_progress = solara.reactive(False)
hysplit_run_refresh = solara.reactive(0)
map_selected_arrivals = solara.reactive([])
map_selected_heights = solara.reactive([])

# ---------------------------------------------------------------------
# Sidebar step expand/collapse state (page-layout only; no business logic)
# ---------------------------------------------------------------------

receptor_step_open = solara.reactive(True)
settings_step_open = solara.reactive(True)
help_panel_open = solara.reactive(False)

SIDEBAR_WIDTH = "420px"
PANEL_BACKGROUND = "rgba(255, 255, 255, 0.96)"
PANEL_BORDER = "1px solid rgba(0, 0, 0, 0.12)"


def gfs0p25_download_is_active():
    """Expose the explicit reactive GFS download lifecycle to all controls."""
    _ = gfs0p25_download_refresh.value
    return gfs0p25_download_in_progress.value


def validate_coordinates(
    latitude,
    longitude,
):
    latitude = float(latitude)
    longitude = float(longitude)

    if not -90 <= latitude <= 90:
        raise ValueError(
            "Latitude must be between −90 and 90."
        )

    if not -180 <= longitude <= 180:
        raise ValueError(
            "Longitude must be between −180 and 180."
        )

    return latitude, longitude


def export_trajectory_gis_files(trajectory_csv, output_directory):
    """Create GIS-ready point and line datasets in EPSG:4326."""
    trajectory_csv = Path(trajectory_csv)
    output_directory = Path(output_directory)
    data = pd.read_csv(trajectory_csv)

    required_columns = {
        "run_name",
        "arrival_datetime_utc",
        "arrival_height_m_agl",
        "age_hours",
        "latitude",
        "longitude",
    }
    missing_columns = required_columns.difference(data.columns)
    if missing_columns:
        raise ValueError(
            "Cannot create GIS files; trajectory CSV is missing: "
            + ", ".join(sorted(missing_columns))
        )

    data = data.dropna(subset=["latitude", "longitude"]).copy()
    if data.empty:
        raise ValueError("Cannot create GIS files from an empty trajectory CSV.")

    point_data = gpd.GeoDataFrame(
        data,
        geometry=gpd.points_from_xy(
            data["longitude"],
            data["latitude"],
        ),
        crs="EPSG:4326",
    )

    line_records = []
    for run_name, group in data.groupby("run_name", sort=True):
        group = group.sort_values("age_hours", ascending=False)
        coordinates = list(
            zip(
                group["longitude"].astype(float),
                group["latitude"].astype(float),
            )
        )
        if len(coordinates) < 2:
            continue

        first = group.iloc[0]
        line_records.append({
            "run_name": str(run_name),
            "arrival_datetime_utc": str(first["arrival_datetime_utc"]),
            "arrival_height_m_agl": int(first["arrival_height_m_agl"]),
            "minimum_age_hours": float(group["age_hours"].min()),
            "maximum_age_hours": float(group["age_hours"].max()),
            "number_of_points": int(len(group)),
            "receptor_name": str(first.get("receptor_name", "")),
            "geometry": LineString(coordinates),
        })

    if not line_records:
        raise ValueError("No trajectory lines could be created.")

    line_data = gpd.GeoDataFrame(line_records, crs="EPSG:4326")
    geopackage_path = output_directory / "trajectories.gpkg"
    point_geojson_path = output_directory / "trajectory_points.geojson"
    line_geojson_path = output_directory / "trajectory_lines.geojson"

    if geopackage_path.exists():
        geopackage_path.unlink()

    point_data.to_file(
        geopackage_path,
        layer="trajectory_points",
        driver="GPKG",
        index=False,
    )
    line_data.to_file(
        geopackage_path,
        layer="trajectory_lines",
        driver="GPKG",
        mode="a",
        index=False,
    )
    point_data.to_file(
        point_geojson_path,
        driver="GeoJSON",
        index=False,
    )
    line_data.to_file(
        line_geojson_path,
        driver="GeoJSON",
        index=False,
    )

    return {
        "trajectory_geopackage": str(geopackage_path),
        "trajectory_points_geojson": str(point_geojson_path),
        "trajectory_lines_geojson": str(line_geojson_path),
    }


def create_results_zip(output_directory, result_files):
    """Package the completed trajectory outputs into one ZIP file."""
    output_directory = Path(output_directory).resolve()
    if not output_directory.is_dir():
        raise FileNotFoundError(
            f"Result directory not found: {output_directory}"
        )

    zip_path = output_directory / f"{output_directory.name}_all_results.zip"
    with zipfile.ZipFile(
        zip_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for result_file in result_files:
            result_path = Path(result_file).resolve()
            if result_path.parent != output_directory:
                raise RuntimeError(
                    f"Unsafe result file refused: {result_path}"
                )
            if not result_path.is_file():
                raise FileNotFoundError(
                    f"Result file not found: {result_path.name}"
                )
            archive.write(result_path, arcname=result_path.name)

    return str(zip_path)


DEFAULT_RESULTS_ROOT = PERSISTENT_RESULTS_ROOT


def save_results_to_persistent_storage(
    output_directory,
    results_root=DEFAULT_RESULTS_ROOT,
):
    """Copy one completed result package from runtime storage to Drive."""
    output_directory = Path(output_directory).resolve()
    results_root = Path(results_root).resolve()

    if not output_directory.is_dir():
        raise FileNotFoundError(
            f"Completed result directory not found: {output_directory}"
        )

    try:
        results_root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RuntimeError(
            f"The persistent results directory is unavailable: {results_root}"
        ) from error

    destination = results_root / output_directory.name
    destination.mkdir(parents=True, exist_ok=True)

    copied_files = []
    for source_file in sorted(output_directory.iterdir()):
        if not source_file.is_file():
            continue
        destination_file = destination / source_file.name
        temporary_file = destination / f".{source_file.name}.copying"
        temporary_file.unlink(missing_ok=True)
        shutil.copy2(source_file, temporary_file)
        temporary_file.replace(destination_file)
        copied_files.append(destination_file)

    if not copied_files:
        raise RuntimeError(
            f"No result files were available to copy from {output_directory}."
        )

    zip_files = [path for path in copied_files if path.suffix.lower() == ".zip"]
    return {
        "directory": str(destination),
        "files": [str(path) for path in copied_files],
        "file_count": len(copied_files),
        "zip_path": str(zip_files[0]) if zip_files else None,
    }


def _latest_result_mtime(directory):
    """Return the newest modification time inside one result package."""
    directory = Path(directory)
    latest = directory.stat().st_mtime

    for path in directory.rglob("*"):
        try:
            latest = max(latest, path.stat().st_mtime)
        except FileNotFoundError:
            # Another cleanup pass may already have removed this item.
            continue

    return latest


def _delete_result_package_if_expired(directory, root, retention_seconds):
    """
    Delete one direct child of a results root only after it is old enough.

    This safety check prevents the cleanup timer from deleting the results
    root itself or any unrelated directory.
    """
    directory = Path(directory).resolve()
    root = Path(root).resolve()

    if directory.parent != root:
        raise RuntimeError(
            f"Unsafe result cleanup target refused: {directory}"
        )

    if not directory.is_dir():
        return True

    age_seconds = time.time() - _latest_result_mtime(directory)
    if age_seconds < retention_seconds:
        return False

    shutil.rmtree(directory)
    return True


def _schedule_result_package_cleanup(runtime_directory, persistent_directory):
    """
    Expire only this completed result package after the retention window.

    Meteorology and the HYSPLIT installation are never touched here.
    If the same result package is updated again before the timer fires,
    its modification time protects the newer files and another check is
    scheduled for the remaining retention period.
    """
    runtime_directory = Path(runtime_directory).resolve()
    persistent_directory = Path(persistent_directory).resolve()

    def cleanup_when_expired():
        remaining_delays = []

        for directory, root in (
            (runtime_directory, RUNTIME_ROOT / "results"),
            (persistent_directory, PERSISTENT_RESULTS_ROOT),
        ):
            try:
                deleted = _delete_result_package_if_expired(
                    directory=directory,
                    root=root,
                    retention_seconds=RESULT_RETENTION_SECONDS,
                )

                if not deleted and directory.is_dir():
                    age_seconds = time.time() - _latest_result_mtime(directory)
                    remaining_delays.append(
                        max(60.0, RESULT_RETENTION_SECONDS - age_seconds)
                    )
            except Exception:
                # Cleanup must never interfere with trajectory execution.
                continue

        if remaining_delays:
            retry_timer = threading.Timer(
                max(remaining_delays),
                cleanup_when_expired,
            )
            retry_timer.daemon = True
            retry_timer.start()

    cleanup_timer = threading.Timer(
        RESULT_RETENTION_SECONDS,
        cleanup_when_expired,
    )
    cleanup_timer.daemon = True
    cleanup_timer.start()


def prune_gdas1_cache(
    current_plan,
    directory=RUNTIME_ROOT / "meteorology" / "gdas1",
):
    """Keep only the GDAS1 files required by the current plan."""
    if not isinstance(current_plan, dict) or not isinstance(
        current_plan.get("files"),
        list,
    ):
        raise ValueError("current_plan is not a valid GDAS1 plan.")

    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    required_filenames = {
        item["filename"]
        for item in current_plan["files"]
    }
    deleted_files = []
    preserved_files = []
    freed_bytes = 0

    for file_path in sorted(directory.glob("gdas1.*")):
        file_path = file_path.resolve()
        if file_path.parent != directory:
            raise RuntimeError(
                f"Unsafe GDAS1 cache target refused: {file_path}"
            )
        if not file_path.is_file():
            continue

        if file_path.name in required_filenames:
            preserved_files.append(file_path.name)
            continue

        size_bytes = file_path.stat().st_size
        file_path.unlink()
        freed_bytes += size_bytes
        deleted_files.append(file_path.name)

    return {
        "directory": directory,
        "deleted_files": deleted_files,
        "preserved_files": preserved_files,
        "deleted_count": len(deleted_files),
        "preserved_count": len(preserved_files),
        "freed_bytes": freed_bytes,
        "freed_megabytes": freed_bytes / 1024**2,
    }


def reset_workflow_for_new_receptor():
    """Invalidate every setting and result tied to the old receptor."""
    applied_trajectory_settings.set(None)
    trajectory_settings_message.set("")
    trajectory_settings_error.set("")
    settings_step_open.set(True)

    gdas1_download_message.set("")
    gdas1_download_error.set("")
    gdas1_download_summary.set(None)
    gdas1_download_in_progress.set(False)
    gdas1_download_refresh.set(gdas1_download_refresh.value + 1)

    gfs0p25_download_message.set("")
    gfs0p25_download_error.set("")
    gfs0p25_download_summary.set(None)
    gfs0p25_download_in_progress.set(False)
    gfs0p25_download_refresh.set(
        gfs0p25_download_refresh.value + 1
    )

    hysplit_run_message.set("")
    hysplit_run_error.set("")
    hysplit_run_summary.set(None)
    hysplit_run_in_progress.set(False)

    map_selected_arrivals.set([])
    map_selected_heights.set([])


def validate_receptor_change_is_safe():
    """Do not change receptor ownership during an active operation."""
    if (gdas1_download_in_progress.value or gfs0p25_download_is_active()):
        raise RuntimeError(
            "Wait for the meteorology download to finish before changing "
            "the receptor."
        )
    if hysplit_run_in_progress.value:
        raise RuntimeError(
            "Wait for the HYSPLIT calculation to finish before changing "
            "the receptor."
        )


def _prepare_lakecci_map_selection():
    """Open a clean LakeCCI selection session on the persistent map."""
    map_selection_active.set(True)
    map_lake_candidate.set(None)
    map_selected_lake_id.set(None)
    map_view_lakes.set([])
    map_loaded_bounds_signature.set(None)
    map_view_lakes_outdated.set(False)
    map_selection_error.set("")

    receptor = applied_receptor.value
    if receptor is not None:
        map_selection_center.set((
            float(receptor["latitude"]),
            float(receptor["longitude"]),
        ))
        map_selection_zoom.set(7)

    map_selection_message.set(
        "Move or zoom the map if needed, then click LOAD LAKES IN "
        "CURRENT MAP VIEW. Select a load polygon and apply it."
    )


def change_selection_mode(value):
    """Keep the receptor form and persistent map in the same mode."""
    try:
        validate_receptor_change_is_safe()
        selection_mode.set(value)
        error_message.set(None)

        if value == "Select LakeCCI lake on map":
            _prepare_lakecci_map_selection()
        elif value == "Manual coordinates":
            receptor = applied_receptor.value
            if receptor is not None:
                location = (
                    float(receptor["latitude"]),
                    float(receptor["longitude"]),
                )
                manual_marker_location.set(location)
                latitude_input.set(location[0])
                longitude_input.set(location[1])
            manual_map_editing.set(True)
            map_selection_active.set(False)
        else:
            map_selection_active.set(False)
    except Exception as error:
        error_message.set(str(error))


def load_lakecci_lake():
    """Load and apply one LakeCCI lake."""
    try:
        validate_receptor_change_is_safe()
        lake_id = lake_id_input.value.strip()

        if not lake_id:
            raise ValueError(
                "Enter a LakeCCI ID."
            )

        lake = load_lake_by_id(
            geopackage_path=LAKE_GPKG,
            lake_id=lake_id,
        )

        latitude = round(
            lake.latitude,
            6,
        )

        longitude = round(
            lake.longitude,
            6,
        )

        latitude_input.set(latitude)
        longitude_input.set(longitude)
        manual_map_editing.set(True)
        reset_workflow_for_new_receptor()

        applied_receptor.set({
            "name": f"Lake {lake.lake_id}",
            "lake_id": lake.lake_id,
            "latitude": latitude,
            "longitude": longitude,
            "method": lake.receptor_method,
            "geometry_type": (
                lake.geometry.geom_type
            ),
            "geometry_geojson": (
                lake.geometry.__geo_interface__
            ),
        })

        status_message.set(
            "LakeCCI receptor loaded and applied."
        )

        error_message.set(None)
        receptor_step_open.set(False)

    except Exception as error:
        status_message.set(None)
        error_message.set(str(error))


def apply_manual_location():
    """Apply coordinates that are not assigned to a LakeCCI lake."""
    try:
        validate_receptor_change_is_safe()
        name = manual_name_input.value.strip()

        if not name:
            name = "Manual receptor"

        latitude, longitude = (
            validate_coordinates(
                latitude_input.value,
                longitude_input.value,
            )
        )
        manual_marker_location.set((latitude, longitude))
        reset_workflow_for_new_receptor()

        applied_receptor.set({
            "name": name,
            "lake_id": None,
            "latitude": latitude,
            "longitude": longitude,
            "method": "manual coordinates",
            "geometry_type": None,
            "geometry_geojson": None,
        })
        manual_map_editing.set(False)

        status_message.set(
            "Independent manual receptor applied."
        )

        error_message.set(None)
        receptor_step_open.set(False)

    except Exception as error:
        status_message.set(None)
        error_message.set(str(error))


def update_manual_latitude(value):
    """Update the latitude field and the marker as one coordinate pair."""
    latitude_input.set(value)
    try:
        latitude, longitude = validate_coordinates(
            value,
            longitude_input.value,
        )
        manual_marker_location.set((latitude, longitude))
        manual_map_editing.set(True)
        error_message.set(None)
    except Exception:
        # Keep the last valid marker position while the user is typing.
        pass


def update_manual_longitude(value):
    """Update the longitude field and the marker as one coordinate pair."""
    longitude_input.set(value)
    try:
        latitude, longitude = validate_coordinates(
            latitude_input.value,
            value,
        )
        manual_marker_location.set((latitude, longitude))
        manual_map_editing.set(True)
        error_message.set(None)
    except Exception:
        # Keep the last valid marker position while the user is typing.
        pass


def update_manual_marker_location(location):
    """Atomically synchronize both fields after the marker is dragged."""
    try:
        if hysplit_run_in_progress.value or (gdas1_download_in_progress.value or gfs0p25_download_is_active()):
            return
        if isinstance(location, dict):
            location = location.get("new", location.get("location"))
        if location is None or len(location) != 2:
            return
        latitude, longitude = validate_coordinates(
            location[0],
            location[1],
        )
        new_location = (round(latitude, 6), round(longitude, 6))
        current_location = tuple(
            float(value) for value in manual_marker_location.value
        )
        if all(
            abs(new_value - current_value) < 1e-9
            for new_value, current_value in zip(
                new_location,
                current_location,
            )
        ):
            return
        manual_marker_location.set(new_location)
        latitude_input.set(new_location[0])
        longitude_input.set(new_location[1])
        manual_map_editing.set(True)
        error_message.set(None)
    except Exception as error:
        error_message.set(str(error))


def _map_bounds_signature(bounds):
    """Create a stable comparison key for Leaflet map bounds."""
    if not bounds or len(bounds) != 2:
        return None
    return tuple(
        round(float(value), 4)
        for corner in bounds
        for value in corner
    )


def update_map_selection_bounds(bounds):
    map_selection_bounds.set(bounds)
    loaded_signature = map_loaded_bounds_signature.value
    if (
        loaded_signature is not None
        and _map_bounds_signature(bounds) != loaded_signature
    ):
        map_view_lakes_outdated.set(True)


def load_lakes_in_current_map_view():
    """Load a bounded LakeCCI subset for the current visible map extent."""
    if map_lakes_loading.value:
        return
    map_lakes_loading.set(True)
    map_selection_message.set(
        "LOADING... Reading LakeCCI polygons from the current map view."
    )
    map_selection_error.set("")
    try:
        if int(map_selection_zoom.value) < 5:
            raise ValueError(
                "Zoom to level 5 or closer before loading LakeCCI polygons."
            )
        lakes = load_lakes_in_bounds(
            geopackage_path=LAKE_GPKG,
            bounds=map_selection_bounds.value,
            maximum_features=300,
            simplify_tolerance=0.001,
        )
        map_view_lakes.set(lakes)
        map_lake_candidate.set(None)
        map_selected_lake_id.set(None)
        map_loaded_bounds_signature.set(
            _map_bounds_signature(map_selection_bounds.value)
        )
        map_view_lakes_outdated.set(False)
        map_selection_message.set(
            f"Loaded {len(lakes)} LakeCCI polygon(s) from the current "
            "map view. Select an exact GeoPackage ID below."
        )
        map_selection_error.set("")
    except Exception as error:
        map_view_lakes.set([])
        map_lake_candidate.set(None)
        map_selected_lake_id.set(None)
        map_selection_message.set("")
        map_selection_error.set(str(error))
    finally:
        map_lakes_loading.set(False)


def preview_map_selected_lake(lake_id):
    """Load the exact selected GeoPackage feature for confirmation."""
    try:
        map_selected_lake_id.set(lake_id)
        if not lake_id:
            map_lake_candidate.set(None)
            return
        lake = load_lake_by_id(LAKE_GPKG, lake_id)
        map_lake_candidate.set({
            "name": f"Lake {lake.lake_id}",
            "lake_id": lake.lake_id,
            "latitude": round(lake.latitude, 6),
            "longitude": round(lake.longitude, 6),
            "method": lake.receptor_method,
            "geometry_type": lake.geometry.geom_type,
            "geometry_geojson": lake.geometry.__geo_interface__,
        })
        map_selection_message.set(
            f"Candidate {lake.lake_id} selected. Review the highlighted "
            "polygon, then click APPLY SELECTED LAKE."
        )
        map_selection_error.set("")
    except Exception as error:
        map_lake_candidate.set(None)
        map_selection_message.set("")
        map_selection_error.set(str(error))


def preview_clicked_lake(*args, **kwargs):
    """Resolve a clicked GeoJSON feature to its exact LakeCCI ID."""
    feature = kwargs.get("feature")
    properties = kwargs.get("properties")
    for argument in args:
        if isinstance(argument, dict):
            if argument.get("type") == "Feature":
                feature = argument
            elif "lake_id" in argument:
                properties = argument
    if feature is not None:
        properties = feature.get("properties", properties)
    lake_id = (properties or {}).get("lake_id")
    if lake_id:
        preview_map_selected_lake(str(lake_id))


def apply_map_selected_lake():
    """Confirm the candidate selected on the LakeCCI map."""
    try:
        validate_receptor_change_is_safe()
        candidate = map_lake_candidate.value
        if candidate is None:
            raise ValueError(
                "Load the visible LakeCCI polygons and select a lake first."
            )

        latitude_input.set(candidate["latitude"])
        longitude_input.set(candidate["longitude"])
        lake_id_input.set(candidate["lake_id"])
        reset_workflow_for_new_receptor()
        applied_receptor.set(dict(candidate))
        map_selection_active.set(False)
        status_message.set(
            "Map-selected LakeCCI receptor applied."
        )
        error_message.set(None)
        map_selection_error.set("")
        receptor_step_open.set(False)
    except Exception as error:
        status_message.set(None)
        error_message.set(str(error))


def begin_new_map_selection():
    """Return the existing receptor preview to LakeCCI selection mode."""
    try:
        validate_receptor_change_is_safe()
        _prepare_lakecci_map_selection()
    except Exception as error:
        error_message.set(str(error))


def begin_manual_map_editing():
    """Return an applied manual receptor to draggable edit mode."""
    try:
        validate_receptor_change_is_safe()
        receptor = applied_receptor.value
        if receptor is not None and receptor.get("lake_id") is None:
            location = (
                round(float(receptor["latitude"]), 6),
                round(float(receptor["longitude"]), 6),
            )
            manual_marker_location.set(location)
            latitude_input.set(location[0])
            longitude_input.set(location[1])
        manual_map_editing.set(True)
        status_message.set(
            "Manual receptor editing enabled. Drag the marker, then "
            "click APPLY MANUAL LOCATION."
        )
        error_message.set(None)
    except Exception as error:
        error_message.set(str(error))


def cancel_map_selection():
    """Return to the applied receptor without invalidating its results."""
    map_selection_active.set(False)
    map_selection_error.set("")
    map_selection_message.set("")
    error_message.set(None)


@solara.component
def AppliedReceptorSummary():
    if error_message.value:
        solara.Error(
            error_message.value
        )
        return

    if status_message.value:
        solara.Success(
            status_message.value
        )

    if applied_receptor.value is None:
        solara.Info(
            "Select and apply a receptor."
        )
        return

    receptor = applied_receptor.value

    lake_id_text = (
        f"`{receptor['lake_id']}`"
        if receptor["lake_id"] is not None
        else "None — independent location"
    )

    solara.Markdown(
        f"""
### Applied receptor

- **Name:** {receptor["name"]}
- **LakeCCI ID:** {lake_id_text}
- **Latitude:** `{receptor["latitude"]:.6f}`
- **Longitude:** `{receptor["longitude"]:.6f}`
- **Method:** {receptor["method"]}
- **Geometry:** {receptor["geometry_type"] or "not applicable"}
        """
    )


@solara.component
def LakeCCIMapSelectionControls(applied_receptor=None):
    """Render LakeCCI selection actions in the scrollable receptor panel."""
    controls_locked = (
        hysplit_run_in_progress.value
        or gdas1_download_in_progress.value
        or gfs0p25_download_is_active()
    )

    solara.Info(
        "Move or zoom the preview map, then load the LakeCCI polygons "
        "for the visible area and click a load polygon."
    )
    solara.Button(
        (
            "LOADING LAKECCI POLYGONS..."
            if map_lakes_loading.value
            else "LOAD LAKES IN CURRENT MAP VIEW"
        ),
        on_click=load_lakes_in_current_map_view,
        color="secondary",
        disabled=map_lakes_loading.value or controls_locked,
        block=True,
    )
    solara.ProgressLinear(value=map_lakes_loading.value)

    if map_view_lakes_outdated.value:
        solara.Warning(
            "The map has moved since these polygons were loaded. "
            "Reload lakes to match the current visible area."
        )

    if map_lakes_loading.value:
        solara.Info(map_selection_message.value)
    elif map_selection_error.value:
        solara.Warning(map_selection_error.value)
    elif map_selection_message.value:
        solara.Success(map_selection_message.value)

    loaded_lake_ids = [lake["lake_id"] for lake in map_view_lakes.value]
    if loaded_lake_ids:
        solara.Select(
            label="Visible LakeCCI polygon ID (fallback)",
            value=map_selected_lake_id.value,
            values=loaded_lake_ids,
            on_value=preview_map_selected_lake,
        )

    candidate = map_lake_candidate.value
    if candidate is not None:
        solara.Markdown(
            f"""
**Candidate LakeCCI ID:** `{candidate["lake_id"]}`  
**Receptor coordinates:** `{candidate["latitude"]:.6f}, {candidate["longitude"]:.6f}`  
**Geometry:** `{candidate["geometry_type"]}`
"""
        )

    solara.Button(
        "APPLY SELECTED LAKE",
        on_click=apply_map_selected_lake,
        color="primary",
        disabled=candidate is None or controls_locked,
        block=True,
    )
    if applied_receptor is not None:
        solara.Button(
            "CANCEL MAP SELECTION",
            on_click=cancel_map_selection,
            color="secondary",
            disabled=controls_locked,
            block=True,
        )


@solara.component
def ReceptorMap(applied_receptor=None, run_summary=None):
    with solara.Card(
        "Receptor preview",
        style={
            "isolation": "isolate",
            "overflow": "hidden",
            "height": "100%",
            "min-height": "0",
            "display": "flex",
            "flex-direction": "column",
            "box-sizing": "border-box",
        },
    ):
        if (
            selection_mode.value == "Manual coordinates"
            and manual_map_editing.value
        ):
            solara.Info(
                "Enter latitude and longitude above or drag the marker. "
                "Click APPLY MANUAL LOCATION to confirm the receptor."
            )
            try:
                preview_latitude, preview_longitude = validate_coordinates(
                    manual_marker_location.value[0],
                    manual_marker_location.value[1],
                )
                ManualReceptorMapElement(
                    latitude=preview_latitude,
                    longitude=preview_longitude,
                    on_location=update_manual_marker_location,
                    locked=(
                        hysplit_run_in_progress.value
                        or (gdas1_download_in_progress.value or gfs0p25_download_is_active())
                    ),
                )
            except Exception as error:
                solara.Warning(str(error))
                EmptyReceptorMapElement()
            return

        if (
            selection_mode.value == "Manual coordinates"
            and not manual_map_editing.value
            and receptor_step_open.value
        ):
            solara.Button(
                (
                    "LOCKED WHILE HYSPLIT IS RUNNING..."
                    if hysplit_run_in_progress.value
                    else "EDIT MANUAL RECEPTOR ON MAP"
                ),
                on_click=begin_manual_map_editing,
                color="secondary",
                disabled=(
                    hysplit_run_in_progress.value
                    or (gdas1_download_in_progress.value or gfs0p25_download_is_active())
                ),
                block=True,
            )

        if (
            selection_mode.value == "Select LakeCCI lake on map"
            and map_selection_active.value
        ):
            solara.Info(
                "Select a LakeCCI polygon on the map. Selection actions are "
                "available in the Receptor location panel."
            )
            LakeSelectionMapElement(
                visible_lakes=map_view_lakes.value,
                candidate=map_lake_candidate.value,
                center=map_selection_center.value,
                zoom=map_selection_zoom.value,
                on_center=map_selection_center.set,
                on_zoom=map_selection_zoom.set,
                on_bounds=update_map_selection_bounds,
                on_lake_click=preview_clicked_lake,
            )
            return

        if (
            selection_mode.value == "Select LakeCCI lake on map"
            and not map_selection_active.value
            and receptor_step_open.value
        ):
            solara.Info(
                "Use the Receptor location panel to start a new LakeCCI "
                "map selection."
            )

        if applied_receptor is None:
            solara.Info(
                "Load a LakeCCI lake or apply manual coordinates to "
                "update the map."
            )
            EmptyReceptorMapElement()
        else:
            trajectory_csv_path = (
                run_summary.get("trajectory_csv")
                if run_summary is not None
                else None
            )
            arrival_options, height_options = trajectory_filter_options(
                trajectory_csv_path
            )

            with solara.Column(style={"position": "relative", "width": "100%"}):
                ReceptorMapElement(
                    name=applied_receptor["name"],
                    latitude=applied_receptor["latitude"],
                    longitude=applied_receptor["longitude"],
                    lake_id=applied_receptor.get("lake_id"),
                    method=applied_receptor["method"],
                    geometry_geojson=applied_receptor.get("geometry_geojson"),
                    trajectory_csv_path=trajectory_csv_path,
                    selected_arrival_times=map_selected_arrivals.value,
                    selected_heights_m_agl=map_selected_heights.value,
                )

                if run_summary is not None:
                    with solara.Column(
                        style={
                            "position": "absolute",
                            "top": "12px",
                            "right": "12px",
                            "z-index": "1000",
                            "background-color": PANEL_BACKGROUND,
                            "border": PANEL_BORDER,
                            "border-radius": "10px",
                            "padding": "10px 12px",
                            "max-width": "260px",
                            "box-shadow": "0 2px 8px rgba(0, 0, 0, 0.18)",
                        }
                    ):
                        solara.Markdown("**Trajectory display filters**")
                        solara.SelectMultiple(
                            label="Arrival times (UTC)",
                            values=map_selected_arrivals,
                            all_values=arrival_options,
                        )
                        solara.SelectMultiple(
                            label="Arrival heights (m AGL)",
                            values=map_selected_heights,
                            all_values=height_options,
                        )
                        if (
                            not map_selected_arrivals.value
                            or not map_selected_heights.value
                        ):
                            solara.Info(
                                "Select at least one arrival time and one "
                                "height to display trajectories."
                            )

                    legend_colours = {
                        500: "#1b9e77",
                        1000: "#d95f02",
                        1500: "#7570b3",
                    }
                    legend_items = "<br>".join(
                        (
                            f'<span style="color:{legend_colours.get(height, "#0072B2")};">'
                            f'●</span>&nbsp; {height} m AGL'
                        )
                        for height in map_selected_heights.value
                    )
                    with solara.Column(
                        style={
                            "position": "absolute",
                            "bottom": "12px",
                            "left": "12px",
                            "z-index": "1000",
                            "background-color": PANEL_BACKGROUND,
                            "border": PANEL_BORDER,
                            "border-radius": "10px",
                            "padding": "12px 14px",
                            "width": "220px",
                            "box-shadow": "0 2px 8px rgba(0, 0, 0, 0.18)",
                        }
                    ):
                        solara.Markdown(
                            f"""
                        **Trajectory heights**

                        {legend_items}

                        **Map symbols**

                        <span style="
                            display:inline-block;
                            width:8px;
                            height:8px;
                            border:1px solid #333;
                            border-radius:50%;
                            background:white;
                            vertical-align:middle;
                            margin-right:8px;
                        "></span> 6-hour endpoint  
                        <span style="
                            display:inline-block;
                            width:12px;
                            height:12px;
                            border:2px solid #333;
                            border-radius:50%;
                            background:white;
                            vertical-align:middle;
                            margin-right:8px;
                        "></span> 24-hour endpoint  
                        ★ &nbsp; Lake receptor
                        """,
                            unsafe_solara_execute=True,
                        )
                        if len(map_selected_arrivals.value) == 1:
                            solara.Markdown(
                                "*Age labels shown every 24 hours.*"
                            )

@solara.component
def ReceptorForm():
    receptor_controls_locked = (
        hysplit_run_in_progress.value
        or (gdas1_download_in_progress.value or gfs0p25_download_is_active())
    )
    with solara.Card("Receptor location"):
        if receptor_controls_locked:
            solara.Info(
                "Receptor controls are locked until the active "
                "meteorology download or HYSPLIT calculation finishes."
            )

        solara.Select(
            label="Selection method",
            value=selection_mode.value,
            values=[
                "LakeCCI lake",
                "Select LakeCCI lake on map",
                "Manual coordinates",
            ],
            on_value=change_selection_mode,
            disabled=receptor_controls_locked,
        )

        if selection_mode.value == "LakeCCI lake":
            solara.Markdown(
                "### Select from LakeCCI"
            )

            solara.InputText(
                label="LakeCCI ID",
                value=lake_id_input,
                disabled=receptor_controls_locked,
            )

            solara.Button(
                "Load and apply LakeCCI lake",
                on_click=load_lakecci_lake,
                color="primary",
                disabled=receptor_controls_locked,
                block=True,
            )

            solara.Info(
                "The receptor and polygon will be loaded "
                "from the LakeCCI GeoPackage."
            )

        elif selection_mode.value == "Select LakeCCI lake on map":
            solara.Markdown("### Select from the LakeCCI map")
            if map_selection_active.value:
                LakeCCIMapSelectionControls(applied_receptor.value)
            else:
                solara.Info(
                    "The selected LakeCCI receptor is now shown in the "
                    "preview map. Click CHANGE RECEPTOR to choose another lake."
                )

        else:
            solara.Markdown(
                "### Independent manual location"
            )

            solara.InputText(
                label="Receptor name",
                value=manual_name_input,
                disabled=receptor_controls_locked,
            )

            solara.InputFloat(
                label="Latitude",
                value=latitude_input.value,
                on_value=update_manual_latitude,
                disabled=receptor_controls_locked,
            )

            solara.InputFloat(
                label="Longitude",
                value=longitude_input.value,
                on_value=update_manual_longitude,
                disabled=receptor_controls_locked,
            )

            solara.Button(
                "Apply manual location",
                on_click=apply_manual_location,
                color="primary",
                disabled=receptor_controls_locked,
                block=True,
            )

            solara.Info(
                "This location will not be assigned "
                "to a LakeCCI lake."
            )

        AppliedReceptorSummary()

@solara.component
def TrajectorySettings(applied_receptor=None):

    def apply_settings():
        if (gdas1_download_in_progress.value or gfs0p25_download_is_active()):
            trajectory_settings_error.set(
                "Wait for the meteorology download to finish before applying "
                "different trajectory settings."
            )
            return
        if hysplit_run_in_progress.value:
            trajectory_settings_error.set(
                "Wait for HYSPLIT to finish before applying different "
                "trajectory settings."
            )
            return

        trajectory_settings_error.set("")
        trajectory_settings_message.set("")
        applied_trajectory_settings.set(None)
        gdas1_download_message.set("")
        gdas1_download_error.set("")
        gdas1_download_summary.set(None)
        gdas1_download_in_progress.set(False)

        gfs0p25_download_message.set("")
        gfs0p25_download_error.set("")
        gfs0p25_download_summary.set(None)
        gfs0p25_download_in_progress.set(False)
        gfs0p25_download_refresh.set(
            gfs0p25_download_refresh.value + 1
        )

        hysplit_run_message.set("")
        hysplit_run_error.set("")
        hysplit_run_summary.set(None)
        hysplit_run_in_progress.set(False)
        map_selected_arrivals.set([])
        map_selected_heights.set([])

        if applied_receptor is None:
            trajectory_settings_error.set(
                "Apply a receptor location first."
            )
            return

        if arrival_date.value is None:
            trajectory_settings_error.set(
                "Select an arrival date."
            )
            return

        if arrival_date.value > dt.date.today():
            trajectory_settings_error.set(
                "The arrival date cannot be in the future."
            )
            return

        if not selected_arrival_hours.value:
            trajectory_settings_error.set(
                "Select at least one arrival hour."
            )
            return

        if backward_duration.value is None:
            trajectory_settings_error.set(
                "Enter the backward duration."
            )
            return

        if not 1 <= int(backward_duration.value) <= 315:
            trajectory_settings_error.set(
                "Backward duration must be between 1 and 315 hours."
            )
            return

        if not selected_heights.value:
            trajectory_settings_error.set(
                "Select at least one arrival height."
            )
            return

        arrival_datetimes = [
            dt.datetime.combine(
                arrival_date.value,
                dt.time(hour=int(hour)),
            )
            for hour in sorted(selected_arrival_hours.value)
        ]

        number_of_model_runs = (
            len(arrival_datetimes)
            * len(selected_heights.value)
        )

        selected_meteorology = METEOROLOGY_DATASET_OPTIONS[
            meteorology_dataset.value
        ]

        # Each dataset has its own non-downloading file planner.
        # The proven GDAS1 workflow remains unchanged.
        gdas1_plan = None
        gfs0p25_plan = None
        if selected_meteorology == "GDAS1":
            gdas1_plan = required_gdas1_files(
                arrival_datetimes=arrival_datetimes,
                backward_duration_hours=int(backward_duration.value),
            )
        elif selected_meteorology == "GFS0P25":
            gfs0p25_plan = required_gfs0p25_files(
                arrival_datetimes=arrival_datetimes,
                backward_duration_hours=int(backward_duration.value),
            )

        configuration = {
            "receptor_name": applied_receptor["name"],
            "lake_id": applied_receptor.get("lake_id"),
            "latitude": float(applied_receptor["latitude"]),
            "longitude": float(applied_receptor["longitude"]),

            "arrival_date": arrival_date.value,

            "arrival_hours_utc": sorted(
                int(hour)
                for hour in selected_arrival_hours.value
            ),

            "arrival_datetimes": arrival_datetimes,

            "backward_duration_hours": int(
                backward_duration.value
            ),

            "hysplit_duration": -int(
                backward_duration.value
            ),

            "heights_m_agl": sorted(
                int(height)
                for height in selected_heights.value
            ),

            "direction": "backward",

            "vertical_motion_name": vertical_motion.value,

            "vertical_motion_code": (
                VERTICAL_MOTION_OPTIONS[
                    vertical_motion.value
                ]
            ),

            "height_reference": "AGL",

            "model_top_metres": int(
                model_top_metres.value
            ),

            "meteorology": selected_meteorology,
            "meteorology_label": meteorology_dataset.value,
            "gdas1_plan": gdas1_plan,
            "gfs0p25_plan": gfs0p25_plan,

            "number_of_arrival_times": len(
                arrival_datetimes
            ),

            "number_of_model_runs": number_of_model_runs,
        }

        applied_trajectory_settings.set(configuration)

        trajectory_settings_message.set(
            f"Configuration validated: "
            f"{len(arrival_datetimes)} arrival times, "
            f"{len(selected_heights.value)} heights and "
            f"{number_of_model_runs} independent HYSPLIT runs."
        )
        settings_step_open.set(False)

    with solara.Card("Trajectory settings"):

        solara.Markdown(
            """
Select when the air masses arrive at the receptor and configure the
backward-trajectory calculation.
"""
        )

        solara.lab.InputDate(
            value=arrival_date,
            label="Arrival date",
            date_format="%Y-%m-%d",
            first_day_of_the_week=1,
            min_date="2004-01-01",
            max_date=dt.date.today().isoformat(),
        )

        solara.SelectMultiple(
            label="Arrival hours (UTC)",
            values=selected_arrival_hours,
            all_values=[0, 6, 12, 18],
        )

        solara.InputInt(
            label="Backward duration (hours)",
            value=backward_duration,
        )

        solara.SelectMultiple(
            label="Arrival heights (metres AGL)",
            values=selected_heights,
            all_values=[500, 1000, 1500],
        )
        solara.Select(
            label="Vertical motion method",
            value=vertical_motion,
            values=list(VERTICAL_MOTION_OPTIONS),
        )

        solara.Select(
            label="Meteorological dataset",
            value=meteorology_dataset,
            values=list(METEOROLOGY_DATASET_OPTIONS),
            disabled=(
                (gdas1_download_in_progress.value or gfs0p25_download_is_active())
                or hysplit_run_in_progress.value
            ),
        )

        solara.Markdown(
            """
**Fixed model settings**

- Trajectory direction: **backward**
- Height reference: **metres AGL**
- Model-top height: **10,000 m**
"""
        )

        if METEOROLOGY_DATASET_OPTIONS[
            meteorology_dataset.value
        ] == "GFS0P25":
            solara.Warning(
                "GFS 0.25° file planning and guarded downloading are enabled. "
                "Review the file count and storage estimate before downloading. "
                "HYSPLIT execution becomes available after every required file is verified."
            )

        solara.Info(
            "Apply the settings to validate the configuration. "
            "Meteorological downloading starts only when you press the separate download button."
        )

        solara.Button(
            (
                "LOCKED WHILE HYSPLIT IS RUNNING..."
                if hysplit_run_in_progress.value
                else (
                    "DOWNLOADING METEOROLOGY..."
                    if (gdas1_download_in_progress.value or gfs0p25_download_is_active())
                    else "APPLY TRAJECTORY SETTINGS"
                )
            ),
            on_click=apply_settings,
            color="primary",
            disabled=(
                applied_receptor is None
                or (gdas1_download_in_progress.value or gfs0p25_download_is_active())
                or hysplit_run_in_progress.value
            ),
            style={
                "width": "100%",
                "pointer-events": (
                    "none"
                    if (
                        (gdas1_download_in_progress.value or gfs0p25_download_is_active())
                        or hysplit_run_in_progress.value
                    )
                    else "auto"
                ),
            },
        )

        if applied_receptor is None:
            solara.Warning(
                "Apply a LakeCCI lake or manual receptor first."
            )

        if trajectory_settings_error.value:
            solara.Error(trajectory_settings_error.value)

        if trajectory_settings_message.value:
            solara.Success(trajectory_settings_message.value)

        if applied_trajectory_settings.value is not None:
            settings = applied_trajectory_settings.value

            arrival_lines = "\n".join(
                f"- `{arrival:%Y-%m-%d %H:%M} UTC`"
                for arrival in settings["arrival_datetimes"]
            )

            heights_text = ", ".join(
                str(height)
                for height in settings["heights_m_agl"]
            )

            solara.Markdown(
                f"""
#### Validated configuration

- **Receptor:** `{settings["receptor_name"]}`
- **Coordinates:** `{settings["latitude"]:.6f}, {settings["longitude"]:.6f}`
- **Backward duration:** `{settings["backward_duration_hours"]} hours`
- **HYSPLIT duration:** `{settings["hysplit_duration"]}`
- **Arrival heights:** `{heights_text} m AGL`
- **Meteorology:** `{settings["meteorology_label"]}`
- **Vertical motion:** `{settings["vertical_motion_name"]}`
- **Vertical motion code:** `{settings["vertical_motion_code"]}`
- **Arrival times:** `{settings["number_of_arrival_times"]}`
- **Independent HYSPLIT runs:** `{settings["number_of_model_runs"]}`



**Arrival times**

{arrival_lines}

**Height reference:** metres above ground level (AGL)
**Trajectory direction:** backward
**Meteorological dataset:** {settings["meteorology_label"]}
**Model-top height:** 10,000 m
"""
            )

@solara.component
def ReceptorStepSummary():
    """Compact summary shown in the sidebar once a receptor is applied."""
    receptor = applied_receptor.value

    def edit_receptor():
        try:
            validate_receptor_change_is_safe()
            receptor_step_open.set(True)
            if selection_mode.value == "Select LakeCCI lake on map":
                _prepare_lakecci_map_selection()
            error_message.set(None)
        except Exception as error:
            error_message.set(str(error))

    with solara.Card("Receptor location"):
        lake_id_text = receptor.get("lake_id") or "manual coordinates"
        solara.Markdown(
            f"**{receptor['name']}**  \n"
            f"{lake_id_text} · "
            f"`{receptor['latitude']:.4f}, {receptor['longitude']:.4f}`"
        )
        solara.Button(
            "CHANGE RECEPTOR",
            on_click=edit_receptor,
            color="secondary",
            block=True,
        )


@solara.component
def SettingsStepSummary():
    """Compact summary shown in the sidebar once trajectory settings are applied."""
    settings = applied_trajectory_settings.value
    heights_text = ", ".join(str(height) for height in settings["heights_m_agl"])
    with solara.Card("Trajectory settings"):
        solara.Markdown(
            f"**{settings['number_of_arrival_times']} arrival times · "
            f"{heights_text} m AGL**  \n"
            f"{settings['meteorology_label']} · "
            f"{settings['backward_duration_hours']} h backward"
        )
        solara.Button(
            "CHANGE TRAJECTORY SETTINGS",
            on_click=lambda: settings_step_open.set(True),
            color="secondary",
            block=True,
        )


@solara.component
def LockedStepRow(label, hint):
    """Placeholder row for a workflow step that has not been reached yet."""
    with solara.Row(
        style={
            "align-items": "center",
            "gap": "10px",
            "padding": "10px 4px",
            "opacity": "0.55",
        }
    ):
        solara.Markdown(f"🔒 **{label}** — {hint}")


@solara.component
def WorkflowStatusPill():
    """Single persistent status readout reflecting every background job."""
    in_progress_colours = ("#FFF4E5", "#8A5300")
    ready_colours = ("#E9F0FF", "#1D4ED8")
    done_colours = ("#E7F6EC", "#1B6B3C")

    if hysplit_run_in_progress.value:
        text, (background, colour) = "● Running HYSPLIT…", in_progress_colours
    elif gdas1_download_in_progress.value or gfs0p25_download_is_active():
        short_label = meteorology_dataset.value.split(" (")[0]
        text, (background, colour) = f"● Downloading {short_label}…", in_progress_colours
    elif (
        hysplit_run_summary.value is not None
        and hysplit_run_summary.value.get("status") == "complete"
    ):
        text, (background, colour) = "✓ Results ready", done_colours
    elif applied_trajectory_settings.value is not None:
        _, _, current_meteorology_status = active_meteorology_status(
            applied_trajectory_settings.value
        )
        if current_meteorology_status["all_ready"]:
            text = "Step 4 of 4 · run HYSPLIT"
        else:
            text = "Step 3 of 4 · meteorology"
        background, colour = ready_colours
    elif applied_receptor.value is not None:
        text, (background, colour) = "Step 2 of 4 · trajectory settings", ready_colours
    else:
        text, (background, colour) = "Step 1 of 4 · receptor", ready_colours

    solara.Text(
        text,
        style={
            "display": "inline-flex",
            "align-items": "center",
            "justify-content": "center",
            "background-color": background,
            "color": colour,
            "padding": "4px 14px",
            "border-radius": "999px",
            "font-size": "13px",
            "font-weight": "600",
            "line-height": "1.35",
            "min-height": "30px",
            "white-space": "nowrap",
        },
    )


@solara.component
def Page():
    solara.Title(APP_TITLE)

    # This layout deliberately uses one browser viewport. The workflow and
    # map therefore never compete for the document scroll position: only the
    # left workflow column scrolls on desktop, while the map stays visible.
    solara.Style(
        """
        .laketraj-header {
            position: fixed;
            z-index: 1000;
            top: 0;
            right: 0;
            left: 0;
            height: 72px;
            box-sizing: border-box;
            background: white;
        }
        .laketraj-workspace {
            position: fixed;
            z-index: 1;
            top: 72px;
            right: 0;
            bottom: 0;
            left: 0;
            width: 100vw;
            height: auto !important;
            min-height: 0;
            min-width: 0;
            overflow: hidden !important;
        }
        .laketraj-sidebar {
            position: absolute !important;
            top: 0;
            bottom: 0;
            left: 0;
            width: 420px;
            height: 100% !important;
            min-height: 0;
            max-height: none !important;
            min-width: 0;
            display: block !important;
            overflow-y: scroll !important;
            overflow-x: hidden !important;
            overscroll-behavior: contain;
            scrollbar-gutter: stable;
            -webkit-overflow-scrolling: touch;
            touch-action: pan-y;
        }
        .laketraj-sidebar > div {
            min-height: min-content;
        }
        .laketraj-map-pane {
            position: absolute !important;
            top: 0;
            right: 0;
            bottom: 0;
            left: 420px;
            width: auto !important;
            height: 100% !important;
            min-height: 0;
            overflow: hidden;
        }
        .laketraj-map-pane > div {
            width: 100%;
            height: auto;
            min-height: 0;
        }
        .laketraj-help-panel {
            position: fixed !important;
            z-index: 1100;
            top: 82px;
            right: 24px;
            width: min(560px, calc(100vw - 48px));
            max-height: calc(100vh - 106px);
            overflow-y: auto;
        }
        @media (max-width: 900px) {
            .laketraj-workspace {
                position: relative;
                inset: auto;
                height: auto;
                min-height: 0;
                overflow: visible;
            }
            .laketraj-header {
                position: relative;
                height: auto;
            }
            .laketraj-sidebar {
                position: relative;
                inset: auto;
                flex: 1 1 auto;
                width: 100% !important;
                height: auto !important;
                max-height: none !important;
                overflow: visible !important;
                border-right: 0 !important;
                border-bottom: 1px solid rgba(0, 0, 0, 0.12);
            }
            .laketraj-map-pane {
                inset: auto;
                width: 100%;
                height: auto !important;
                min-height: 560px;
                position: relative;
                overflow: visible;
            }
            .laketraj-help-panel {
                position: relative !important;
                inset: auto;
                width: auto;
                max-height: none;
                overflow: visible;
            }
        }
        """
    )

    with solara.Column(style={"max-width": "100%", "padding": "0"}):

        # -----------------------------------------------------------
        # Header: title + one persistent status pill + help toggle
        # -----------------------------------------------------------
        with solara.Row(
            classes=["laketraj-header"],
            style={
                "align-items": "center",
                "justify-content": "space-between",
                "padding": "14px 24px",
                "border-bottom": "1px solid rgba(0, 0, 0, 0.12)",
                "flex-wrap": "wrap",
                "gap": "10px",
            }
        ):
            with solara.Row(style={"align-items": "baseline", "gap": "10px"}):
                solara.Markdown(f"### {APP_TITLE}")
                solara.Text(
                    f"v{APP_VERSION}",
                    style={"font-size": "12px", "color": "rgba(0, 0, 0, 0.5)"},
                )

            with solara.Row(style={"align-items": "center", "gap": "10px"}):
                WorkflowStatusPill()
                solara.Button(
                    "Hide help" if help_panel_open.value else "How to use LakeTraj",
                    on_click=lambda: help_panel_open.set(not help_panel_open.value),
                    color="secondary",
                    style={"height": "32px"},
                )

        if help_panel_open.value:
            with solara.Card(
                classes=["laketraj-help-panel"],
                style={"margin": "0"},
            ):
                solara.Markdown(
                    """
1. Select and apply a **LakeCCI lake** or an independent **manual receptor**.
2. Choose the arrival date, hours, heights, duration and vertical-motion method, then apply the trajectory settings.
3. With **GDAS1**, download or verify the required meteorological files.
   With **GFS 0.25°**, download and verify the required daily files.
4. Run the HYSPLIT backward trajectories and keep the page open until completion.
5. Explore the trajectories on the receptor map and download the required CSV, GeoJSON, GeoPackage or ZIP outputs.

**Note:** Meteorology files remain in persistent runtime storage.
Completed trajectory outputs are saved automatically to persistent storage.
                    """
                )

        # -----------------------------------------------------------
        # Body: collapsible workflow steps (left) + persistent map (right)
        # -----------------------------------------------------------
        with solara.Row(
            classes=["laketraj-workspace"],
            style={
                "align-items": "stretch",
                "gap": "0",
                "flex-wrap": "nowrap",
                "width": "100%",
            },
        ):

            with solara.Column(
                classes=["laketraj-sidebar"],
                style={
                    "width": SIDEBAR_WIDTH,
                    "position": "absolute",
                    "top": "0",
                    "bottom": "0",
                    "left": "0",
                    "height": "100%",
                    "min-height": "0",
                    "max-height": "none",
                    "overflow-y": "scroll",
                    "overflow-x": "hidden",
                    "padding": "16px",
                    "border-right": "1px solid rgba(0, 0, 0, 0.12)",
                    "box-sizing": "border-box",
                    "-webkit-overflow-scrolling": "touch",
                }
            ):
                if applied_receptor.value is not None and not receptor_step_open.value:
                    ReceptorStepSummary()
                else:
                    ReceptorForm()

                if applied_receptor.value is None:
                    LockedStepRow(
                        "Trajectory settings",
                        "apply a receptor first",
                    )
                elif (
                    applied_trajectory_settings.value is not None
                    and not settings_step_open.value
                ):
                    SettingsStepSummary()
                else:
                    TrajectorySettings(applied_receptor.value)

                if applied_trajectory_settings.value is not None:
                    if (
                        applied_trajectory_settings.value["meteorology"]
                        == "GDAS1"
                    ):
                        MeteorologyPlanCard(
                            applied_trajectory_settings.value["gdas1_plan"]
                        )
                        HysplitCalculationCard(
                            applied_trajectory_settings.value
                        )
                        ResultsDownloadCard(hysplit_run_summary.value)
                    else:
                        Gfs0p25PlanCard(
                            applied_trajectory_settings.value["gfs0p25_plan"]
                        )
                        Gfs0p25DownloadCard(
                            applied_trajectory_settings.value["gfs0p25_plan"]
                        )
                        HysplitCalculationCard(
                            applied_trajectory_settings.value
                        )
                        ResultsDownloadCard(hysplit_run_summary.value)
                else:
                    LockedStepRow(
                        "Meteorology",
                        "apply trajectory settings first",
                    )
                    LockedStepRow(
                        "Run HYSPLIT & results",
                        "prepare the required meteorology first",
                    )

            with solara.Column(
                classes=["laketraj-map-pane"],
                style={
                    "flex": "1",
                    "min-width": "0",
                    "padding": "16px",
                    "box-sizing": "border-box",
                },
            ):
                ReceptorMap(
                    applied_receptor.value,
                    hysplit_run_summary.value,
                )
@solara.component
def Gfs0p25PlanCard(plan):
    total_gib = plan["estimated_total_size_bytes"] / (1024 ** 3)
    with solara.Card("Required GFS 0.25° meteorology files"):
        solara.Markdown(
            f"""
The backward-trajectory period requires daily GFS 0.25° ARL files from:

- **Start:** `{plan['meteorology_start']:%Y-%m-%d %H:%M UTC}`
- **End:** `{plan['meteorology_end']:%Y-%m-%d %H:%M UTC}`
- **Required daily files:** `{len(plan['files'])}`
- **Estimated storage:** `{total_gib:.2f} GiB`
"""
        )
        solara.Warning(
            "GFS 0.25° daily files are large. Confirm that Docker has "
            "enough free storage before downloading."
        )
        for item in plan["files"]:
            size_gib = item["estimated_size_bytes"] / (1024 ** 3)
            solara.Markdown(
                f"**{item['filename']}** — about {size_gib:.2f} GiB  "
                f"[Open NOAA public archive object]({item['url']})"
            )
@solara.component
def Gfs0p25DownloadCard(plan):
    # Accessing this value refreshes the card after the callback completes.
    _ = gfs0p25_download_refresh.value

    status = inspect_gfs0p25_files(plan)
    storage = gfs0p25_storage_status(plan)
    running = gfs0p25_download_is_active()

    def run_download_in_background():
        """Prepare the large GFS files without blocking Solara's UI callback."""
        try:
            cleanup = prune_gfs0p25_cache(plan)
            result = download_required_gfs0p25_files(plan)
            final_status = inspect_gfs0p25_files(plan)

            downloaded = sum(
                item["status"] == "downloaded"
                for item in result["files"]
            )
            reused = sum(
                item["status"] == "already_exists"
                for item in result["files"]
            )
            failed = sum(
                item["status"] == "failed"
                for item in result["files"]
            )

            gfs0p25_download_summary.set({
                "downloaded": downloaded,
                "reused": reused,
                "failed": failed,
                "deleted_count": cleanup["deleted_count"],
                "freed_bytes": cleanup["freed_bytes"],
                "all_ready": final_status["all_ready"],
            })

            if final_status["all_ready"]:
                gfs0p25_download_message.set(
                    "All required GFS 0.25° files are downloaded and verified."
                )
            else:
                gfs0p25_download_message.set("")
                gfs0p25_download_error.set(
                    "Some GFS files failed verification. Press download "
                    "again to retry only the missing files."
                )

        except Exception as error:
            gfs0p25_download_message.set("")
            gfs0p25_download_error.set(str(error))

        finally:
            # Match the proven GDAS lifecycle: always release every UI lock
            # and force all subscribed components to inspect the cache again.
            gfs0p25_download_in_progress.set(False)
            gfs0p25_download_refresh.set(
                gfs0p25_download_refresh.value + 1
            )

    def start_download():
        if (
            gfs0p25_download_in_progress.value
            or hysplit_run_in_progress.value
        ):
            return

        gfs0p25_download_in_progress.set(True)
        gfs0p25_download_message.set(
            "DOWNLOADING... Please keep this page open."
        )
        gfs0p25_download_error.set("")
        gfs0p25_download_summary.set(None)

        try:
            worker = threading.Thread(
                target=run_download_in_background,
                name="laketraj-gfs0p25-download",
                daemon=True,
            )
            worker.start()
        except Exception as error:
            gfs0p25_download_message.set("")
            gfs0p25_download_error.set(
                f"Could not start the GFS download worker: {error}"
            )
            gfs0p25_download_in_progress.set(False)
            gfs0p25_download_refresh.set(
                gfs0p25_download_refresh.value + 1
            )

    with solara.Card("GFS 0.25° download and verification"):
        solara.Markdown(
            f"""
- **Runtime directory:** `{status['directory']}`
- **Free storage:** `{storage['free_bytes'] / 1024**3:.2f} GiB`
- **Estimated missing download:** `{storage['missing_estimated_bytes'] / 1024**3:.2f} GiB`
- **Required including 10% reserve:** `{storage['required_with_reserve_bytes'] / 1024**3:.2f} GiB`
"""
        )
        for item in status["files"]:
            if item["ready"]:
                state = f"✅ Verified — {item['size_bytes'] / 1024**3:.2f} GiB"
            elif item["exists"]:
                state = "⚠️ Present but unverified; it will be downloaded again"
            else:
                state = "⬇️ Not downloaded"
            solara.Markdown(f"**{item['filename']}** — {state}")

        if status["all_ready"]:
            solara.Success(
                "All required GFS files are downloaded and verified. "
                "You can now run the HYSPLIT backward trajectories below."
            )
        elif not storage["enough_space"]:
            solara.Error(
                "The runtime does not currently have enough free storage for "
                "the missing GFS files plus the safety reserve."
            )
        else:
            solara.Warning(
                "Downloading several gigabytes can take a long time. Keep "
                "Docker and this page open until verification finishes."
            )

        solara.Button(
            (
                "LOCKED — HYSPLIT RUNNING..."
                if hysplit_run_in_progress.value
                else (
                    "DOWNLOADING GFS 0.25° FILES..."
                    if running
                    else (
                        "VERIFY & CLEAN GFS 0.25° FILES"
                        if status["all_ready"]
                        else "DOWNLOAD GFS 0.25° FILES"
                    )
                )
            ),
            on_click=start_download,
            color="primary",
            disabled=(
                running
                or hysplit_run_in_progress.value
                or (
                    not status["all_ready"]
                    and not storage["enough_space"]
                )
            ),
            block=True,
        )
        solara.ProgressLinear(value=running)
        if running:
            solara.Info("Downloading files sequentially. Completed files are retained safely.")
        elif gfs0p25_download_summary.value is not None:
            result = gfs0p25_download_summary.value
            solara.Markdown(
                f"**Preparation result:** {result['downloaded']} downloaded, "
                f"{result['reused']} reused, {result['failed']} failed; "
                f"{result['deleted_count']} obsolete cache items removed; "
                f"{result['freed_bytes'] / 1024**3:.2f} GiB released."
            )
            if result["all_ready"]:
                solara.Success("All required GFS 0.25° files passed verification.")

        if not running and gfs0p25_download_error.value:
            solara.Error(gfs0p25_download_error.value)


@solara.component
def MeteorologyPlanCard(plan):
    """Display and asynchronously download the required GDAS1 files."""

    # Reading the reactive value causes this component to refresh after the
    # worker completes.
    _ = gdas1_download_refresh.value

    file_status = inspect_gdas1_files(plan)
    running = gdas1_download_in_progress.value

    def run_download_in_background():
        cache_cleanup = {
            "deleted_count": 0,
            "freed_megabytes": 0.0,
        }

        try:
            cache_cleanup = prune_gdas1_cache(plan)

            result = download_required_gdas1_files(plan)

            # Always inspect the files on disk after the downloader returns.
            # A downloader may return without raising while one or more files
            # are still incomplete or invalid.
            final_status = inspect_gdas1_files(plan)

            result_files = result.get("files", [])

            downloaded_count = sum(
                item.get("status") == "downloaded"
                for item in result_files
            )
            reused_count = sum(
                item.get("status") == "already_exists"
                for item in result_files
            )

            required_count = len(plan.get("files", []))
            ready_count = sum(
                bool(item.get("ready"))
                for item in final_status.get("files", [])
            )
            failed_count = max(0, required_count - ready_count)

            gdas1_download_summary.set({
                "required": required_count,
                "ready": ready_count,
                "downloaded": downloaded_count,
                "reused": reused_count,
                "failed": failed_count,
                "removed_unneeded": cache_cleanup["deleted_count"],
                "released_megabytes": cache_cleanup["freed_megabytes"],
            })

            if final_status.get("all_ready", False):
                gdas1_download_message.set(
                    "GDAS1 cache cleaned and all required meteorological "
                    "files were downloaded and verified."
                )
                gdas1_download_error.set("")
            else:
                gdas1_download_message.set("")
                gdas1_download_error.set(
                    "Some GDAS1 files are missing, incomplete, or failed "
                    "verification. Press the download button again to retry."
                )

        except Exception as error:
            # Try to report the actual on-disk state without masking the
            # original exception if inspection itself fails.
            try:
                current_status = inspect_gdas1_files(plan)
                current_files = current_status.get("files", [])
                ready_count = sum(
                    bool(item.get("ready"))
                    for item in current_files
                )
            except Exception:
                ready_count = 0

            required_count = len(plan.get("files", []))

            gdas1_download_summary.set({
                "required": required_count,
                "ready": ready_count,
                "downloaded": None,
                "reused": None,
                "failed": max(0, required_count - ready_count),
                "removed_unneeded": cache_cleanup["deleted_count"],
                "released_megabytes": cache_cleanup["freed_megabytes"],
            })

            gdas1_download_message.set("")
            gdas1_download_error.set(
                f"GDAS1 download failed: {error}"
            )

        finally:
            # These updates must happen even when downloading or verification
            # raises an exception.
            gdas1_download_in_progress.set(False)
            gdas1_download_refresh.set(
                gdas1_download_refresh.value + 1
            )

            # The lock is acquired by start_download().
            gdas1_download_lock.release()

    def start_download(*_event_args):
        """Start GDAS1 preparation without blocking the Solara UI thread."""

        if hysplit_run_in_progress.value:
            gdas1_download_error.set(
                "GDAS1 files cannot be changed while HYSPLIT is running. "
                "Wait for the trajectory calculation to finish."
            )
            return

        if (
            gdas1_download_in_progress.value
            or gfs0p25_download_is_active()
        ):
            return

        # The reactive flag normally prevents duplicate clicks. The lock
        # additionally protects against two callbacks arriving nearly
        # simultaneously.
        if not gdas1_download_lock.acquire(blocking=False):
            return

        gdas1_download_in_progress.set(True)
        gdas1_download_summary.set(None)
        gdas1_download_message.set(
            "DOWNLOADING... Please keep this page open."
        )
        gdas1_download_error.set("")

        try:
            worker = threading.Thread(
                target=run_download_in_background,
                name="laketraj-gdas1-download",
                daemon=True,
            )
            worker.start()

        except Exception as error:
            # If the thread cannot be created, release everything here because
            # the worker's finally block will never execute.
            gdas1_download_in_progress.set(False)
            gdas1_download_message.set("")
            gdas1_download_error.set(
                f"Could not start the GDAS1 download worker: {error}"
            )
            gdas1_download_refresh.set(
                gdas1_download_refresh.value + 1
            )
            gdas1_download_lock.release()

    with solara.Card("Required GDAS1 meteorology files"):
        solara.Markdown(
            f"""
The backward-trajectory period requires meteorological data from:

- **Start:** `{plan["meteorology_start"]:%Y-%m-%d %H:%M UTC}`
- **End:** `{plan["meteorology_end"]:%Y-%m-%d %H:%M UTC}`
- **Archive:** `GDAS1`
- **Runtime directory:** `{file_status["directory"]}`
- **Required files:** `{len(plan["files"])}`
"""
        )

        for item in file_status["files"]:
            if item["ready"]:
                status_text = (
                    f'✅ Ready — {item["size_bytes"] / 1024**2:.2f} MB'
                )
            elif item["exists"]:
                status_text = (
                    "⚠️ Present but unverified; it will be downloaded again"
                )
            else:
                status_text = "⬇️ Not downloaded"

            solara.Markdown(
                f"""
**{item["filename"]}**

- Status: {status_text}
- <a href="{item["url"]}" target="_blank" rel="noopener noreferrer" title="Open the NOAA archive file in a new tab">Open NOAA archive file</a>
"""
            )

        if file_status["all_ready"]:
            solara.Success(
                "All required GDAS1 meteorological files are ready. "
                "Use the button below to verify and remove unrelated files."
            )
        else:
            solara.Info(
                "Download the required files before running HYSPLIT. "
                "Files are stored in the persistent local runtime cache."
            )

        solara.Button(
            (
                "LOCKED — HYSPLIT RUNNING..."
                if hysplit_run_in_progress.value
                else (
                    "DOWNLOADING GDAS1 FILES..."
                    if running
                    else (
                        "VERIFY & CLEAN GDAS1 FILES"
                        if file_status["all_ready"]
                        else "DOWNLOAD GDAS1 FILES"
                    )
                )
            ),
            on_click=start_download,
            color="primary",
            disabled=(
                running
                or hysplit_run_in_progress.value
                or gfs0p25_download_is_active()
            ),
            block=True,
        )

        solara.ProgressLinear(value=running)

        if running:
            solara.Info(
                gdas1_download_message.value
                or "Preparing GDAS1 files. Please keep this page open."
            )
        elif gdas1_download_message.value:
            solara.Success(gdas1_download_message.value)

        if gdas1_download_error.value:
            solara.Error(gdas1_download_error.value)

        if (
            not running
            and gdas1_download_summary.value is not None
        ):
            summary = gdas1_download_summary.value

            solara.Markdown(
                f"""
#### GDAS1 preparation summary

- **Required files:** `{summary["required"]}`
- **Files ready:** `{summary["ready"]}`
- **Newly downloaded:** `{summary["downloaded"] if summary["downloaded"] is not None else "unknown"}`
- **Already available:** `{summary["reused"] if summary["reused"] is not None else "unknown"}`
- **Failed files:** `{summary["failed"]}`
- **Unneeded cached files removed:** `{summary["removed_unneeded"]}`
- **Cache storage released:** `{summary["released_megabytes"]:.2f} MB`
"""
            )

def active_meteorology_status(configuration):
    """Return plan, cache inspection, and directory for the selected dataset."""
    dataset = configuration["meteorology"]
    if dataset == "GDAS1":
        plan = configuration["gdas1_plan"]
        status = inspect_gdas1_files(plan)
        label = "GDAS1"
    elif dataset == "GFS0P25":
        plan = configuration["gfs0p25_plan"]
        status = inspect_gfs0p25_files(plan)
        label = "GFS 0.25°"
    else:
        raise ValueError(f"Unsupported meteorological dataset: {dataset!r}")
    return label, plan, status


@solara.component
def HysplitCalculationCard(configuration):

    # The meteorology download callback increments this value after
    # completion, causing this card to re-check readiness automatically.
    _ = gdas1_download_refresh.value
    _ = gfs0p25_download_refresh.value
    _ = hysplit_run_refresh.value

    meteorology_label, meteorology_plan, meteorology_status = (
        active_meteorology_status(configuration)
    )

    environment_status = validate_hysplit_environment(
        meteorology_plan=meteorology_plan,
        meteorology_directory=meteorology_status["directory"],
    )
    calculation_complete = (
        hysplit_run_summary.value is not None
        and hysplit_run_summary.value.get("status") == "complete"
    )
    run_ready = (
        meteorology_status["all_ready"]
        and environment_status["ready"]
    )

    def run_hysplit_worker():
        """Runs the validated HYSPLIT batch on a background thread."""
        try:
            batch_result = run_trajectory_batch(
                configuration=configuration,
                overwrite=True,
            )

            if batch_result["status"] != "complete":
                failed_runs = [
                    result
                    for result in batch_result["run_results"]
                    if result["status"] != "complete"
                    or not result["parsed"]
                ]

                failure_text = "; ".join(
                    (
                        f'{result["run_name"]}: '
                        f'{result["status"]}'
                    )
                    for result in failed_runs
                )

                raise RuntimeError(
                    "One or more HYSPLIT runs failed: "
                    f"{failure_text}"
                )

            saved_results = save_batch_results(
                batch_result=batch_result,
                configuration=configuration,
            )

            gis_results = export_trajectory_gis_files(
                trajectory_csv=saved_results["trajectory_csv"],
                output_directory=saved_results["output_directory"],
            )
            results_zip = create_results_zip(
                output_directory=saved_results["output_directory"],
                result_files=[
                    saved_results["trajectory_csv"],
                    saved_results["run_summary_csv"],
                    saved_results["metadata_json"],
                    gis_results["trajectory_geopackage"],
                    gis_results["trajectory_points_geojson"],
                    gis_results["trajectory_lines_geojson"],
                ],
            )

            persistent_results = save_results_to_persistent_storage(
                saved_results["output_directory"]
            )

            _, _, cached_meteorology_status = active_meteorology_status(
                configuration
            )
            cached_meteorology_files = sum(
                item["ready"]
                for item in cached_meteorology_status["files"]
            )
            cached_meteorology_megabytes = sum(
                item["size_bytes"]
                for item in cached_meteorology_status["files"]
                if item["ready"]
            ) / 1024**2

            summary = {
                "status": batch_result["status"],
                "meteorology_dataset": configuration["meteorology"],
                "meteorology_label": configuration["meteorology_label"],
                "number_of_runs": (
                    batch_result["number_of_runs"]
                ),
                "successful_runs": (
                    batch_result["successful_runs"]
                ),
                "failed_runs": batch_result["failed_runs"],
                "number_of_points": (
                    batch_result["number_of_points"]
                ),
                "output_directory": str(
                    saved_results["output_directory"]
                ),
                "trajectory_csv": str(
                    saved_results["trajectory_csv"]
                ),
                "run_summary_csv": str(
                    saved_results["run_summary_csv"]
                ),
                "metadata_json": str(
                    saved_results["metadata_json"]
                ),
                "trajectory_geopackage": (
                    gis_results["trajectory_geopackage"]
                ),
                "trajectory_points_geojson": (
                    gis_results["trajectory_points_geojson"]
                ),
                "trajectory_lines_geojson": (
                    gis_results["trajectory_lines_geojson"]
                ),
                "results_zip": results_zip,
                "persistent_results_directory": persistent_results["directory"],
                "persistent_results_zip": persistent_results["zip_path"],
                "persistent_result_file_count": persistent_results["file_count"],
                "cached_meteorology_files": cached_meteorology_files,
                "cached_meteorology_megabytes": cached_meteorology_megabytes,
            }

            arrival_options, height_options = trajectory_filter_options(
                summary["trajectory_csv"]
            )
            map_selected_arrivals.set(arrival_options)
            map_selected_heights.set(height_options)
            hysplit_run_summary.set(summary)

            # Keep the working meteorology cache unchanged. Only completed
            # trajectory result packages are scheduled for expiration.
            _schedule_result_package_cleanup(
                runtime_directory=saved_results["output_directory"],
                persistent_directory=persistent_results["directory"],
            )

            hysplit_run_message.set(
                f'HYSPLIT completed successfully: '
                f'{summary["successful_runs"]} runs and '
                f'{summary["number_of_points"]} trajectory points.'
            )

            # The required files remain cached for later compatible runs.
            # Refresh both cards so their on-disk status stays current.
            gdas1_download_refresh.set(
                gdas1_download_refresh.value + 1
            )
            gfs0p25_download_refresh.set(
                gfs0p25_download_refresh.value + 1
            )

        except Exception as error:
            hysplit_run_message.set("")
            hysplit_run_error.set(str(error))

        finally:
            # Match the proven GDAS1/GFS0P25 lifecycle: only release the UI
            # lock and force a re-check once the real work has finished, not
            # before the worker thread has even started.
            hysplit_run_in_progress.set(False)
            hysplit_run_refresh.set(hysplit_run_refresh.value + 1)

    def run_hysplit():
        if calculation_complete:
            hysplit_run_error.set(
                "These trajectories have already been calculated. "
                "Change and apply the receptor or trajectory settings "
                "before starting another calculation."
            )
            return

        if (gdas1_download_in_progress.value or gfs0p25_download_is_active()):
            hysplit_run_error.set(
                f"HYSPLIT cannot start while {meteorology_label} files are still "
                f"being prepared. Wait for the files to be ready, then "
                "start the trajectory calculation."
            )
            return

        # Validate the current files at click time instead of relying on
        # the button's previously rendered disabled state in the browser.
        current_label, current_plan, current_meteorology_status = (
            active_meteorology_status(configuration)
        )
        current_environment_status = validate_hysplit_environment(
            meteorology_plan=current_plan,
            meteorology_directory=current_meteorology_status["directory"],
        )

        hysplit_run_error.set("")

        if not current_meteorology_status["all_ready"]:
            hysplit_run_error.set(
                f"HYSPLIT did not start because one or more required "
                f"{current_label} files are missing, unverified, or empty."
            )
            return

        if not current_environment_status["ready"]:
            hysplit_run_error.set(
                "HYSPLIT did not start:\n"
                + "\n".join(current_environment_status["errors"])
            )
            return

        hysplit_run_in_progress.set(True)
        hysplit_run_message.set(
            "HYSPLIT trajectory calculation started. "
            "Please keep this page open."
        )
        hysplit_run_error.set("")
        hysplit_run_summary.set(None)

        # Run the expensive HYSPLIT batch outside the Solara event callback,
        # exactly like the GDAS1/GFS0P25 downloads: start a worker thread and
        # return immediately, so the click handler never blocks and never
        # re-renders this card mid-callback.
        try:
            threading.Thread(
                target=run_hysplit_worker,
                daemon=True,
                name="laketraj-hysplit-run",
            ).start()
        except Exception as error:
            hysplit_run_in_progress.set(False)
            hysplit_run_error.set(
                f"Could not start HYSPLIT trajectory calculation: {error}"
            )
            hysplit_run_refresh.set(hysplit_run_refresh.value + 1)

    configuration_heading = (
        "The following validated configuration is being executed:"
        if hysplit_run_in_progress.value
        else (
            "The completed calculation used:"
            if calculation_complete
            else "The validated configuration will execute:"
        )
    )

    with solara.Card("Run HYSPLIT"):
        solara.Markdown(
            f"""
{configuration_heading}

- **Independent runs:** `{configuration["number_of_model_runs"]}`
- **Arrival times:** `{configuration["number_of_arrival_times"]}`
- **Arrival heights:** `{len(configuration["heights_m_agl"])}`
- **Backward duration:** `{configuration["backward_duration_hours"]} hours`
- **Vertical motion:** `{configuration["vertical_motion_name"]}`
"""
        )

        if not meteorology_status["all_ready"]:
            solara.Warning(
                f"Download and verify all required {meteorology_label} files "
                f"before running HYSPLIT."
            )

        elif not environment_status["ready"]:
            for error_message in environment_status["errors"]:
                solara.Error(error_message)

        else:
            solara.Success(
                "HYSPLIT and all required meteorological files "
                "are ready."
            )

        solara.Info(
            f"Required {meteorology_label} files remain in the local cache "
            "for reuse after trajectory execution."
        )
        solara.Button(
            (
                f"DOWNLOADING {meteorology_label.upper()}..."
                if (gdas1_download_in_progress.value or gfs0p25_download_is_active())
                else (
                    "RUNNING HYSPLIT..."
                    if hysplit_run_in_progress.value
                    else (
                        "CHANGE SETTINGS TO RE-RUN"
                        if calculation_complete
                        else (
                            f"DOWNLOAD {meteorology_label.upper()} FILES FIRST"
                            if not meteorology_status["all_ready"]
                            else (
                                "HYSPLIT NOT READY"
                                if not environment_status["ready"]
                                else "RUN BACKWARD TRAJECTORIES"
                            )
                        )
                    )
                )
            ),
            on_click=run_hysplit,
            color="primary",
            disabled=(
                hysplit_run_in_progress.value
                or calculation_complete
                or (gdas1_download_in_progress.value or gfs0p25_download_is_active())
                or not run_ready
            ),
            style={
                "width": "100%",
                # Text wrapping and dynamic height fixes
                "white-space": "normal",
                "height": "auto",
                "min-height": "36px",
                "padding": "8px 12px",
                "word-break": "break-word",
                "line-height": "1.3",
                # Keep the event binding stable, but block pointer clicks
                # in the browser throughout GDAS1 preparation.
                "pointer-events": (
                    "none"
                    if (
                        (gdas1_download_in_progress.value or gfs0p25_download_is_active())
                        or not run_ready
                    )
                    else "auto"
                ),
                "opacity": (
                    "0.55"
                    if (
                        (gdas1_download_in_progress.value or gfs0p25_download_is_active())
                        or not run_ready
                    )
                    else "1"
                ),
            },
        )

        solara.ProgressLinear(
            value=hysplit_run_in_progress.value
        )

        if hysplit_run_in_progress.value:
            solara.Info(hysplit_run_message.value)

        elif hysplit_run_message.value:
            solara.Success(hysplit_run_message.value)

        if hysplit_run_error.value:
            solara.Error(hysplit_run_error.value)
      
@solara.component
def ResultsDownloadCard(summary):
    if summary is None or summary.get("status") != "complete":
        return

    # Extract file paths from the run summary
    zip_path = Path(summary["results_zip"]) if summary.get("results_zip") else None
    csv_path = Path(summary["trajectory_csv"]) if summary.get("trajectory_csv") else None
    gpkg_path = Path(summary["trajectory_geopackage"]) if summary.get("trajectory_geopackage") else None

    with solara.Card("Download Trajectory Results"):
        solara.Success(
            f"Calculation complete! Generated {summary.get('number_of_points', 0)} trajectory points."
        )
        solara.Markdown(f"**Saved to:** `{summary.get('output_directory', '')}`")

        # Stack buttons vertically so long labels fit within the sidebar width
        with solara.Column(style={"gap": "8px", "margin-top": "10px"}):
            if zip_path and zip_path.is_file():
                solara.FileDownload(
                    data=zip_path.read_bytes(),
                    filename=zip_path.name,
                    label="DOWNLOAD ALL RESULTS (.ZIP)",
                )
            if csv_path and csv_path.is_file():
                solara.FileDownload(
                    data=csv_path.read_bytes(),
                    filename=csv_path.name,
                    label="DOWNLOAD CSV (.CSV)",
                )
            if gpkg_path and gpkg_path.is_file():
                solara.FileDownload(
                    data=gpkg_path.read_bytes(),
                    filename=gpkg_path.name,
                    label="DOWNLOAD GEOPACKAGE (.GPKG)",
                )