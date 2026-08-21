import datetime as dt
import os
from pathlib import Path
import shutil
import urllib.request

GDAS1_ARCHIVE_URL = (
    "https://www.ready.noaa.gov/data/archives/gdas1"
)
GFS0P25_ARCHIVE_URL = (
    "https://noaa-oar-arl-hysplit-pds.s3.amazonaws.com/gfs0p25"
)
GFS0P25_ESTIMATED_FILE_BYTES = 2_898_905_680


def gdas1_file_for_date(value):
    """
    Return GDAS1 file information for one date.

    GDAS1 weekly periods:
        w1: days 1–7
        w2: days 8–14
        w3: days 15–21
        w4: days 22–28
        w5: days 29–end
    """
    if isinstance(value, dt.datetime):
        value = value.date()

    if not isinstance(value, dt.date):
        raise TypeError(
            "value must be datetime.date or datetime.datetime"
        )

    week = min(5, ((value.day - 1) // 7) + 1)
    month = value.strftime("%b").lower()
    short_year = value.strftime("%y")

    filename = (
        f"gdas1.{month}{short_year}.w{week}"
    )

    return {
        "filename": filename,
        "year": value.year,
        "week": week,
        "url": (
            f"{GDAS1_ARCHIVE_URL}/"
            f"{value.year}/{filename}"
        ),
    }


def required_gdas1_files(
    arrival_datetimes,
    backward_duration_hours,
):
    """
    Calculate the weekly GDAS1 files needed for all arrivals.

    This function does not download any files.
    """
    if not arrival_datetimes:
        raise ValueError(
            "At least one arrival datetime is required."
        )

    arrivals = sorted(arrival_datetimes)

    if not all(
        isinstance(value, dt.datetime)
        for value in arrivals
    ):
        raise TypeError(
            "Every arrival must be datetime.datetime."
        )

    duration = int(backward_duration_hours)

    if duration <= 0:
        raise ValueError(
            "Backward duration must be positive."
        )

    meteorology_start = (
        arrivals[0] - dt.timedelta(hours=duration)
    )

    meteorology_end = arrivals[-1]

    required_files = {}
    current_date = meteorology_start.date()
    final_date = meteorology_end.date()

    while current_date <= final_date:
        file_information = gdas1_file_for_date(
            current_date
        )

        required_files[
            file_information["filename"]
        ] = file_information

        current_date += dt.timedelta(days=1)

    return {
        "meteorology_start": meteorology_start,
        "meteorology_end": meteorology_end,
        "backward_duration_hours": duration,
        "number_of_arrival_times": len(arrivals),
        "files": list(required_files.values()),
    }


def gfs0p25_file_for_date(value):
    """Return NOAA public-archive information for one GFS 0.25° day."""
    if isinstance(value, dt.datetime):
        value = value.date()
    if not isinstance(value, dt.date):
        raise TypeError("value must be datetime.date or datetime.datetime")

    filename = f"{value:%Y%m%d}_gfs0p25"
    object_key = f"gfs0p25/{value:%Y}/{value:%m}/{filename}"
    return {
        "filename": filename,
        "date": value,
        "object_key": object_key,
        "url": f"{GFS0P25_ARCHIVE_URL}/{value:%Y}/{value:%m}/{filename}",
        "estimated_size_bytes": GFS0P25_ESTIMATED_FILE_BYTES,
    }


def required_gfs0p25_files(arrival_datetimes, backward_duration_hours):
    """Plan inclusive daily GFS 0.25° ARL files; do not download them."""
    if not arrival_datetimes:
        raise ValueError("At least one arrival datetime is required.")
    arrivals = sorted(arrival_datetimes)
    if not all(isinstance(value, dt.datetime) for value in arrivals):
        raise TypeError("Every arrival must be datetime.datetime.")
    duration = int(backward_duration_hours)
    if duration <= 0:
        raise ValueError("Backward duration must be positive.")

    meteorology_start = arrivals[0] - dt.timedelta(hours=duration)
    meteorology_end = arrivals[-1]
    files = []
    current_date = meteorology_start.date()
    while current_date <= meteorology_end.date():
        files.append(gfs0p25_file_for_date(current_date))
        current_date += dt.timedelta(days=1)

    return {
        "dataset": "GFS0P25",
        "meteorology_start": meteorology_start,
        "meteorology_end": meteorology_end,
        "backward_duration_hours": duration,
        "number_of_arrival_times": len(arrivals),
        "file_count": len(files),
        "files": files,
        "estimated_total_size_bytes": sum(
            item["estimated_size_bytes"] for item in files
        ),
    }

# ---------------------------------------------------------------------
# Deployment-configurable runtime paths
# ---------------------------------------------------------------------

DEFAULT_RUNTIME_ROOT = Path(
    os.environ.get(
        "LAKETRAJ_RUNTIME_DIR",
        "/content/LakeTraj_runtime",
    )
)

DEFAULT_GFS0P25_DIRECTORY = (
    DEFAULT_RUNTIME_ROOT / "meteorology" / "gfs0p25"
)


def prepare_gfs0p25_directory(directory=DEFAULT_GFS0P25_DIRECTORY):
    """Create and return the temporary runtime GFS 0.25° directory."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _gfs0p25_completion_path(path):
    return Path(str(path) + ".complete")


def _completed_gfs0p25_size(path):
    marker = _gfs0p25_completion_path(path)
    if not path.is_file() or not marker.is_file():
        return None

    actual_size = path.stat().st_size
    if actual_size <= 0:
        return None

    try:
        marker_text = marker.read_text().strip()
    except OSError:
        return None

    # Earlier LakeTraj versions created an empty ``.complete`` marker. The
    # final GFS file is still safe to reuse because downloads are written to a
    # separate ``.part`` path and renamed atomically only after completion.
    # Upgrade that legacy marker in place so all later inspections use the
    # recorded byte-size verification.
    if not marker_text:
        marker.write_text(str(actual_size))
        return actual_size

    try:
        recorded_size = int(marker_text)
    except ValueError:
        recorded_size = None

    if recorded_size != actual_size:
        # A final file accompanied by a completion marker is authoritative:
        # the downloader writes to ``.part`` and performs an atomic rename
        # before creating this marker. Repair stale or malformed marker
        # contents instead of starting another multi-gigabyte download.
        marker.write_text(str(actual_size))

    return actual_size


def inspect_gfs0p25_files(plan, directory=DEFAULT_GFS0P25_DIRECTORY):
    """Inspect cached GFS files without making a network request."""
    _require_plan(plan)
    directory = prepare_gfs0p25_directory(directory)
    statuses = []
    for info in plan["files"]:
        path = directory / info["filename"]
        size = path.stat().st_size if path.is_file() else 0
        completed_size = _completed_gfs0p25_size(path)
        statuses.append({
            **info,
            "path": path,
            "exists": path.is_file(),
            "size_bytes": size,
            "ready": completed_size is not None,
            "verified_size_bytes": completed_size or 0,
        })
    return {
        "directory": directory,
        "files": statuses,
        "all_ready": bool(statuses) and all(item["ready"] for item in statuses),
    }


def gfs0p25_storage_status(plan, directory=DEFAULT_GFS0P25_DIRECTORY):
    """Estimate whether free runtime storage can hold missing GFS files."""
    status = inspect_gfs0p25_files(plan, directory)
    missing_estimate = sum(
        item["estimated_size_bytes"]
        for item in status["files"]
        if not item["ready"]
    )
    free_bytes = shutil.disk_usage(status["directory"]).free
    required_with_reserve = int(missing_estimate * 1.10)
    return {
        **status,
        "missing_estimated_bytes": missing_estimate,
        "required_with_reserve_bytes": required_with_reserve,
        "free_bytes": free_bytes,
        "enough_space": free_bytes >= required_with_reserve,
    }


def download_gfs0p25_file(
    file_info,
    directory=DEFAULT_GFS0P25_DIRECTORY,
    chunk_size=8 * 1024 * 1024,
):
    """Atomically download and verify one daily GFS 0.25° ARL file."""
    directory = prepare_gfs0p25_directory(directory)
    destination = directory / file_info["filename"]
    partial = Path(str(destination) + ".part")
    marker = _gfs0p25_completion_path(destination)

    completed_size = _completed_gfs0p25_size(destination)
    if completed_size is not None:
        return {
            "filename": file_info["filename"],
            "path": destination,
            "status": "already_exists",
            "size_bytes": completed_size,
        }

    partial.unlink(missing_ok=True)
    marker.unlink(missing_ok=True)
    request = urllib.request.Request(
        file_info["url"], headers={"User-Agent": "LakeTraj/1.0"}
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            expected_size = int(response.headers.get("Content-Length", 0))
            required_size = expected_size or file_info["estimated_size_bytes"]
            free_space = shutil.disk_usage(directory).free
            if free_space < int(required_size * 1.10):
                raise RuntimeError(
                    f"Insufficient runtime storage for {file_info['filename']}. "
                    f"Need about {required_size / 1024**3:.2f} GiB plus reserve; "
                    f"only {free_space / 1024**3:.2f} GiB is free."
                )
            with open(partial, "wb") as output:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    output.write(chunk)

        received_size = partial.stat().st_size
        if received_size <= 0:
            raise RuntimeError(f"The downloaded file {file_info['filename']} is empty.")
        if expected_size and received_size != expected_size:
            raise RuntimeError(
                f"Incomplete download for {file_info['filename']}: expected "
                f"{expected_size} bytes but received {received_size}."
            )
        partial.replace(destination)
        marker.write_text(str(received_size))
        return {
            "filename": file_info["filename"],
            "path": destination,
            "status": "downloaded",
            "size_bytes": received_size,
        }
    except Exception:
        partial.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        raise


def download_required_gfs0p25_files(plan, directory=DEFAULT_GFS0P25_DIRECTORY):
    """Download missing GFS files sequentially and retain successful files."""
    _require_plan(plan)
    storage = gfs0p25_storage_status(plan, directory)
    if not storage["enough_space"]:
        raise RuntimeError(
            "Insufficient runtime storage for the missing GFS 0.25° files."
            f"files. Required with reserve: "
            f"{storage['required_with_reserve_bytes'] / 1024**3:.2f} GiB; "
            f"available: {storage['free_bytes'] / 1024**3:.2f} GiB."
        )
    results = []
    for info in plan["files"]:
        try:
            result = download_gfs0p25_file(info, directory)
        except Exception as error:
            result = {
                "filename": info.get("filename", "<unknown>"),
                "path": None,
                "status": "failed",
                "error": str(error),
            }
        results.append(result)
    final_status = inspect_gfs0p25_files(plan, directory)
    return {
        "directory": Path(directory),
        "files": results,
        "all_ready": final_status["all_ready"],
        "failed_files": [
            item["filename"] for item in results if item["status"] == "failed"
        ],
    }


def prune_gfs0p25_cache(plan, directory=DEFAULT_GFS0P25_DIRECTORY):
    """Remove only unneeded GFS cache files, markers, and partial files."""
    _require_plan(plan)
    directory = prepare_gfs0p25_directory(directory)
    required = {item["filename"] for item in plan["files"]}
    deleted = []
    released = 0
    for path in directory.iterdir():
        base_name = path.name.removesuffix(".complete").removesuffix(".part")
        is_gfs_cache_item = base_name.endswith("_gfs0p25")
        if path.is_file() and is_gfs_cache_item and base_name not in required:
            released += path.stat().st_size
            path.unlink()
            deleted.append(path.name)
    return {
        "deleted_files": deleted,
        "deleted_count": len(deleted),
        "freed_bytes": released,
    }


DEFAULT_GDAS1_DIRECTORY = (
    DEFAULT_RUNTIME_ROOT / "meteorology" / "gdas1"
)

def _require_plan(plan, argument_name="plan"):
    """
    Validate that `plan` looks like a meteorology plan dict before any
    function starts indexing into it.

    Every plan-consuming function below used to assume `plan` was always
    a well-formed dict produced by `required_gdas1_files()`. In practice,
    callers sometimes pass through a stale or unset value (for example
    `configuration["gdas1_plan"]` when `configuration` itself is `None`
    because trajectory settings were never applied), which previously
    surfaced as a bare `TypeError: 'NoneType' object is not subscriptable`
    with no indication of what was actually wrong.
    """
    if not isinstance(plan, dict):
        raise TypeError(
            f"{argument_name} must be a dict produced by "
            f"required_gdas1_files(), got {type(plan).__name__} "
            f"({plan!r}). This usually means the upstream configuration "
            "was never applied (e.g. 'APPLY TRAJECTORY SETTINGS' was not "
            "clicked, or the app module was reloaded after it was)."
        )

    if not isinstance(plan.get("files"), list):
        raise ValueError(
            f"{argument_name}['files'] must be a list, got "
            f"{type(plan.get('files')).__name__}. Full plan: {plan!r}"
        )


def prepare_gdas1_directory(directory=DEFAULT_GDAS1_DIRECTORY):
    """Create and return the temporary runtime GDAS1 directory."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def inspect_gdas1_files(
    plan,
    directory=DEFAULT_GDAS1_DIRECTORY,
):
    """Report whether every planned GDAS1 file already exists locally."""
    _require_plan(plan)
    directory = prepare_gdas1_directory(directory)

    file_statuses = []

    for file_info in plan["files"]:
        path = directory / file_info["filename"]
        exists = path.is_file()
        size_bytes = path.stat().st_size if exists else 0

        file_statuses.append(
            {
                **file_info,
                "path": path,
                "exists": exists,
                "size_bytes": size_bytes,
                "ready": exists and size_bytes > 0,
            }
        )

    return {
        "directory": directory,
        "files": file_statuses,
        "all_ready": all(
            item["ready"] for item in file_statuses
        ),
    }


def download_gdas1_file(
    file_info,
    directory=DEFAULT_GDAS1_DIRECTORY,
    chunk_size=1024 * 1024,
):
    """
    Download one GDAS1 file atomically.

    The incomplete download uses a .part suffix and is renamed only
    after the download finishes successfully.
    """
    directory = prepare_gdas1_directory(directory)

    destination = directory / file_info["filename"]
    partial_destination = destination.with_suffix(
        destination.suffix + ".part"
    )

    if destination.is_file() and destination.stat().st_size > 0:
        return {
            "filename": file_info["filename"],
            "path": destination,
            "status": "already_exists",
            "size_bytes": destination.stat().st_size,
        }

    request = urllib.request.Request(
        file_info["url"],
        headers={"User-Agent": "LakeTraj/1.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            expected_size = int(
                response.headers.get("Content-Length", 0)
            )

            if expected_size:
                free_space = shutil.disk_usage(directory).free

                if free_space < expected_size * 1.1:
                    raise RuntimeError(
                        "Insufficient runtime storage for "
                        f'{file_info["filename"]}. '
                        f"Required approximately "
                        f"{expected_size / 1024**3:.2f} GB; "
                        f"available {free_space / 1024**3:.2f} GB."
                    )

            with open(partial_destination, "wb") as output_file:
                while True:
                    chunk = response.read(chunk_size)

                    if not chunk:
                        break

                    output_file.write(chunk)

        downloaded_size = partial_destination.stat().st_size

        if expected_size and downloaded_size != expected_size:
            raise RuntimeError(
                f"Incomplete download for {file_info['filename']}: "
                f"expected {expected_size} bytes but received "
                f"{downloaded_size} bytes."
            )

        partial_destination.replace(destination)

        return {
            "filename": file_info["filename"],
            "path": destination,
            "status": "downloaded",
            "size_bytes": destination.stat().st_size,
        }

    except Exception:
        if partial_destination.exists():
            partial_destination.unlink()

        raise


def download_required_gdas1_files(
    plan,
    directory=DEFAULT_GDAS1_DIRECTORY,
):
    """Download every GDAS1 file required by a meteorology plan.

    A single file's network failure no longer aborts the rest of the
    batch: each file is attempted independently, and any failure is
    captured in that file's result entry (status="failed") rather than
    raised out of the function. Files that already downloaded
    successfully in this call, or in a prior call (see the
    already_exists short-circuit in download_gdas1_file), are left in
    place, so re-calling this after a partial failure only retries what
    is actually missing.
    """
    _require_plan(plan)
    results = []

    for file_info in plan["files"]:
        try:
            result = download_gdas1_file(
                file_info=file_info,
                directory=directory,
            )
        except Exception as error:
            result = {
                "filename": file_info.get("filename", "<unknown>"),
                "path": None,
                "status": "failed",
                "error": str(error),
            }

        results.append(result)

    failed = [
        result["filename"]
        for result in results
        if result["status"] == "failed"
    ]

    return {
        "directory": Path(directory),
        "files": results,
        "all_ready": all(
            result["status"] in {"downloaded", "already_exists"}
            for result in results
        ),
        "failed_files": failed,
    }


def cleanup_obsolete_gdas1_files(
    previous_plan,
    new_plan,
    directory=DEFAULT_GDAS1_DIRECTORY,
):
    """Delete old-plan GDAS1 files that the new plan does not need."""
    _require_plan(previous_plan, "previous_plan")
    _require_plan(new_plan, "new_plan")
    directory = prepare_gdas1_directory(directory).resolve()

    new_filenames = {
        item["filename"]
        for item in new_plan["files"]
    }
    deleted_files = []
    preserved_files = []
    freed_bytes = 0

    for file_info in previous_plan["files"]:
        filename = file_info["filename"]
        if filename in new_filenames:
            preserved_files.append(filename)
            continue

        file_path = (directory / filename).resolve()
        if file_path.parent != directory:
            raise RuntimeError(
                f"Unsafe obsolete-GDAS1 cleanup target refused: {file_path}"
            )

        if file_path.is_file():
            size_bytes = file_path.stat().st_size
            file_path.unlink()
            freed_bytes += size_bytes
            deleted_files.append(filename)

    return {
        "directory": directory,
        "deleted_files": deleted_files,
        "preserved_files": preserved_files,
        "deleted_count": len(deleted_files),
        "preserved_count": len(preserved_files),
        "freed_bytes": freed_bytes,
        "freed_megabytes": freed_bytes / 1024**2,
    }


def cleanup_gdas1_files(
    plan,
    batch_result,
    saved_results,
    directory=DEFAULT_GDAS1_DIRECTORY,
):
    """
    Delete only the GDAS1 files used by a successfully saved batch.
    """
    _require_plan(plan)

    if not isinstance(batch_result, dict):
        raise TypeError(
            "batch_result must be the dict returned by "
            f"run_trajectory_batch(), got {type(batch_result).__name__}."
        )

    if not isinstance(saved_results, dict):
        raise TypeError(
            "saved_results must be the dict returned by "
            f"save_batch_results(), got {type(saved_results).__name__}."
        )

    directory = Path(directory).resolve()

    if batch_result.get("status") != "complete":
        raise RuntimeError(
            "GDAS1 cleanup refused: the HYSPLIT batch is not complete."
        )

    if batch_result.get("failed_runs", 1) != 0:
        raise RuntimeError(
            "GDAS1 cleanup refused: one or more HYSPLIT runs failed."
        )

    required_outputs = [
        Path(saved_results["trajectory_csv"]),
        Path(saved_results["run_summary_csv"]),
        Path(saved_results["metadata_json"]),
    ]

    for output_path in required_outputs:
        if not output_path.is_file():
            raise RuntimeError(
                "GDAS1 cleanup refused because a result file is "
                f"missing: {output_path}"
            )

        if output_path.stat().st_size == 0:
            raise RuntimeError(
                "GDAS1 cleanup refused because a result file is "
                f"empty: {output_path}"
            )

    deleted_files = []
    missing_files = []
    freed_bytes = 0

    for file_info in plan["files"]:
        file_path = (
            directory / file_info["filename"]
        ).resolve()

        # Ensure that only direct children of the expected GDAS1
        # runtime directory can be deleted.
        if file_path.parent != directory:
            raise RuntimeError(
                f"Unsafe GDAS1 cleanup target refused: {file_path}"
            )

        if file_path.is_file():
            size_bytes = file_path.stat().st_size
            file_path.unlink()

            freed_bytes += size_bytes

            deleted_files.append(
                {
                    "filename": file_info["filename"],
                    "path": file_path,
                    "size_bytes": size_bytes,
                }
            )
        else:
            missing_files.append(file_info["filename"])

    return {
        "directory": directory,
        "deleted_files": deleted_files,
        "missing_files": missing_files,
        "deleted_count": len(deleted_files),
        "freed_bytes": freed_bytes,
        "freed_megabytes": freed_bytes / 1024**2,
        "cleanup_complete": True,
    }

