from pathlib import Path
import os
import shutil
import subprocess
import time
import pandas as pd
import json
import re

from .parser import parse_tdump

# ---------------------------------------------------------------------
# Deployment-configurable paths
# ---------------------------------------------------------------------

DEFAULT_HYSPLIT_ROOT = Path(
    os.environ.get(
        "HYSPLIT_HOME",
        "/content/hysplit",
    )
)

DEFAULT_HYSPLIT_EXECUTABLE = (
    DEFAULT_HYSPLIT_ROOT / "exec" / "hyts_std"
)

DEFAULT_ASCDATA_FILE = (
    DEFAULT_HYSPLIT_ROOT / "bdyfiles" / "ASCDATA.CFG"
)


DEFAULT_RUNTIME_ROOT = Path(
    os.environ.get(
        "LAKETRAJ_RUNTIME_DIR",
        "/content/LakeTraj_runtime",
    )
)

DEFAULT_GDAS1_DIRECTORY = (
    DEFAULT_RUNTIME_ROOT / "meteorology" / "gdas1"
)

DEFAULT_GFS0P25_DIRECTORY = (
    DEFAULT_RUNTIME_ROOT / "meteorology" / "gfs0p25"
)

DEFAULT_RUN_DIRECTORY = (
    DEFAULT_RUNTIME_ROOT / "hysplit_runs"
)

DEFAULT_RESULTS_DIRECTORY = (
    DEFAULT_RUNTIME_ROOT / "results"
)

# Keys that functions in this module actually dereference from
# `configuration`. This is intentionally the used subset, not every key
# TrajectorySettings.apply_settings() happens to include (e.g. it omits
# UI-only fields like "arrival_date" and "number_of_model_runs").
_REQUIRED_CONFIGURATION_KEYS = {
    "arrival_datetimes",
    "heights_m_agl",
    "latitude",
    "longitude",
    "hysplit_duration",
    "vertical_motion_code",
    "model_top_metres",
    "receptor_name",
    "direction",
    "backward_duration_hours",
    "vertical_motion_name",
    "meteorology",
}


def _require_configuration(configuration):
    """
    Validate that `configuration` looks like the dict produced by
    TrajectorySettings.apply_settings(), before any function starts
    indexing into it.

    Every configuration-consuming function below used to assume this was
    always a well-formed dict. In practice a caller sometimes passes
    through `app.applied_trajectory_settings.value` while it is still
    `None` (trajectory settings were never applied in the running app,
    or the app module was reloaded after they were), which previously
    surfaced as a bare `TypeError: 'NoneType' object is not subscriptable`
    several calls deep with no indication of what was actually wrong.
    """
    if not isinstance(configuration, dict):
        raise TypeError(
            "configuration must be a dict produced by "
            f"TrajectorySettings, got {type(configuration).__name__} "
            f"({configuration!r}). This usually means trajectory "
            "settings were never applied -- e.g. 'APPLY TRAJECTORY "
            "SETTINGS' was not clicked in the app, or the app module "
            "was reloaded after it was."
        )

    missing = _REQUIRED_CONFIGURATION_KEYS - configuration.keys()

    if missing:
        raise ValueError(
            f"configuration is missing required key(s): {sorted(missing)}"
        )


def meteorology_inputs(configuration):
    """Return the active meteorology plan and runtime directory."""
    _require_configuration(configuration)
    dataset = configuration["meteorology"]
    if dataset == "GDAS1":
        plan = configuration.get("gdas1_plan")
        directory = DEFAULT_GDAS1_DIRECTORY
    elif dataset == "GFS0P25":
        plan = configuration.get("gfs0p25_plan")
        directory = DEFAULT_GFS0P25_DIRECTORY
    else:
        raise ValueError(f"Unsupported meteorological dataset: {dataset!r}")
    if not isinstance(plan, dict) or not isinstance(plan.get("files"), list):
        raise ValueError(
            f"The {dataset} meteorology plan is missing or invalid. "
            "Apply the trajectory settings again."
        )
    return plan, directory


def _directory_control_text(path):
    """Return a directory path with the trailing slash HYSPLIT expects."""
    return str(Path(path).resolve()) + "/"


def prepare_trajectory_run(
    configuration,
    arrival_datetime,
    height_m_agl,
    run_root=DEFAULT_RUN_DIRECTORY,
    overwrite=False,
    environment=None,
):
    """
    Create one isolated HYSPLIT trajectory run directory.

    This prepares CONTROL and ASCDATA.CFG but does not launch HYSPLIT.

    `environment` may be supplied by a caller that has already validated
    the HYSPLIT environment once for this configuration (run_trajectory_batch
    does this, since it calls prepare_trajectory_run once per arrival
    time/height pair and would otherwise re-stat every meteorology file
    on every iteration). When omitted, the environment is validated here,
    so this function stays safe to call directly for a single ad-hoc run.
    """
    _require_configuration(configuration)
    meteorology_plan, meteorology_directory = meteorology_inputs(configuration)

    if environment is None:
        environment = require_hysplit_environment(
            meteorology_plan=meteorology_plan,
            meteorology_directory=meteorology_directory,
        )

    if arrival_datetime not in configuration["arrival_datetimes"]:
        raise ValueError(
            "The requested arrival datetime is not present in the "
            "validated configuration."
        )

    height_m_agl = int(height_m_agl)

    if height_m_agl not in configuration["heights_m_agl"]:
        raise ValueError(
            "The requested height is not present in the validated "
            "configuration."
        )

    run_name = (
        f'{arrival_datetime:%Y%m%d_%H%M}'
        f'_h{height_m_agl:04d}'
    )

    run_directory = Path(run_root) / run_name

    if run_directory.exists():
        if not overwrite:
            raise FileExistsError(
                f"Run directory already exists: {run_directory}. "
                "Use overwrite=True only when this run should be replaced."
            )

        shutil.rmtree(run_directory)

    run_directory.mkdir(parents=True)

    local_ascdata = run_directory / "ASCDATA.CFG"

    shutil.copy2(
        environment["ascdata_file"],
        local_ascdata,
    )

    output_filename = "tdump"

    control_lines = [
        arrival_datetime.strftime("%y %m %d %H"),
        "1",
        (
            f'{configuration["latitude"]:.6f} '
            f'{configuration["longitude"]:.6f} '
            f"{height_m_agl}"
        ),
        str(configuration["hysplit_duration"]),
        str(configuration["vertical_motion_code"]),
        str(configuration["model_top_metres"]),
        str(len(meteorology_plan["files"])),
    ]

    for file_info in meteorology_plan["files"]:
        control_lines.extend(
            [
                _directory_control_text(
                    environment["meteorology_directory"]
                ),
                file_info["filename"],
            ]
        )

    control_lines.extend(
        [
            _directory_control_text(run_directory),
            output_filename,
        ]
    )

    control_path = run_directory / "CONTROL"

    control_path.write_text(
        "\n".join(control_lines) + "\n",
        encoding="utf-8",
    )

    return {
        "run_name": run_name,
        "run_directory": run_directory,
        "control_path": control_path,
        "ascdata_path": local_ascdata,
        "output_path": run_directory / output_filename,
        "arrival_datetime": arrival_datetime,
        "height_m_agl": height_m_agl,
        "status": "prepared",
    }


def execute_trajectory_run(
    prepared_run,
    executable=DEFAULT_HYSPLIT_EXECUTABLE,
    timeout_seconds=600,
):
    """
    Execute one prepared HYSPLIT trajectory run and retain diagnostics.
    """
    executable = Path(executable)
    run_directory = Path(prepared_run["run_directory"])
    control_path = Path(prepared_run["control_path"])
    ascdata_path = Path(prepared_run["ascdata_path"])
    output_path = Path(prepared_run["output_path"])

    if not executable.is_file():
        raise FileNotFoundError(
            f"HYSPLIT executable not found: {executable}"
        )

    if not control_path.is_file():
        raise FileNotFoundError(
            f"CONTROL file not found: {control_path}"
        )

    if not ascdata_path.is_file():
        raise FileNotFoundError(
            f"ASCDATA.CFG not found: {ascdata_path}"
        )

    stdout_path = run_directory / "stdout.txt"
    stderr_path = run_directory / "stderr.txt"
    message_path = run_directory / "MESSAGE"
    warning_path = run_directory / "WARNING"

    start_time = time.monotonic()

    try:
        result = subprocess.run(
            [str(executable)],
            cwd=run_directory,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )

        elapsed_seconds = time.monotonic() - start_time

        stdout_path.write_text(
            result.stdout or "",
            encoding="utf-8",
        )

        stderr_path.write_text(
            result.stderr or "",
            encoding="utf-8",
        )

        output_exists = output_path.is_file()
        output_size = (
            output_path.stat().st_size
            if output_exists
            else 0
        )

        completed = (
            result.returncode == 0
            and output_exists
            and output_size > 0
        )

        return {
            **prepared_run,
            "status": "complete" if completed else "failed",
            "return_code": result.returncode,
            "elapsed_seconds": elapsed_seconds,
            "output_exists": output_exists,
            "output_size_bytes": output_size,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "message_path": message_path,
            "warning_path": warning_path,
            "message_exists": message_path.is_file(),
            "warning_exists": warning_path.is_file(),
        }

    except subprocess.TimeoutExpired as error:
        elapsed_seconds = time.monotonic() - start_time

        stdout_path.write_text(
            error.stdout or "",
            encoding="utf-8",
        )

        stderr_path.write_text(
            error.stderr or "",
            encoding="utf-8",
        )

        return {
            **prepared_run,
            "status": "timeout",
            "return_code": None,
            "elapsed_seconds": elapsed_seconds,
            "output_exists": output_path.is_file(),
            "output_size_bytes": (
                output_path.stat().st_size
                if output_path.is_file()
                else 0
            ),
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "message_path": message_path,
            "warning_path": warning_path,
            "message_exists": message_path.is_file(),
            "warning_exists": warning_path.is_file(),
        }


def run_trajectory_batch(
    configuration,
    run_root=DEFAULT_RUN_DIRECTORY,
    overwrite=True,
):
    """
    Prepare, execute and parse every independent arrival-time/height run.

    A run that raises an unexpected exception while being prepared or
    executed (a disk-full error mid-copy, a transient OSError spawning
    the subprocess, etc.) is recorded as a failed run with status
    "error" and the batch continues with the remaining runs, instead of
    the exception propagating out of this function and discarding every
    run that already completed successfully. Previously, one such
    exception meant this function never returned at all, so even runs
    that had already produced valid trajectories were lost.
    """
    _require_configuration(configuration)

    meteorology_plan, meteorology_directory = meteorology_inputs(configuration)
    environment = require_hysplit_environment(
        meteorology_plan=meteorology_plan,
        meteorology_directory=meteorology_directory,
    )

    run_results = []
    trajectory_frames = []

    for arrival_datetime in configuration["arrival_datetimes"]:
        for height_m_agl in configuration["heights_m_agl"]:

            run_name = (
                f'{arrival_datetime:%Y%m%d_%H%M}'
                f'_h{int(height_m_agl):04d}'
            )

            try:
                prepared_run = prepare_trajectory_run(
                    configuration=configuration,
                    arrival_datetime=arrival_datetime,
                    height_m_agl=height_m_agl,
                    run_root=run_root,
                    overwrite=overwrite,
                    environment=environment,
                )

                executed_run = execute_trajectory_run(
                    prepared_run
                )
            except Exception as error:
                run_results.append(
                    {
                        "run_name": run_name,
                        "run_directory": None,
                        "arrival_datetime": arrival_datetime,
                        "height_m_agl": int(height_m_agl),
                        "status": "error",
                        "return_code": None,
                        "elapsed_seconds": None,
                        "output_exists": False,
                        "output_size_bytes": 0,
                        "parsed": False,
                        "number_of_points": 0,
                        "error": str(error),
                    }
                )
                continue

            run_summary = {
                key: value
                for key, value in executed_run.items()
                if key not in {"configuration"}
            }

            if executed_run["status"] == "complete":
                try:
                    dataframe = parse_tdump(
                        executed_run["output_path"]
                    )

                    arrival_timestamp = pd.Timestamp(
                        arrival_datetime
                    )

                    if arrival_timestamp.tzinfo is None:
                        arrival_timestamp = (
                            arrival_timestamp.tz_localize("UTC")
                        )
                    else:
                        arrival_timestamp = (
                            arrival_timestamp.tz_convert("UTC")
                        )

                    dataframe["run_name"] = (
                        executed_run["run_name"]
                    )

                    dataframe["arrival_datetime_utc"] = (
                        arrival_timestamp
                    )

                    dataframe["arrival_height_m_agl"] = int(
                        height_m_agl
                    )

                    dataframe["receptor_name"] = (
                        configuration["receptor_name"]
                    )

                    dataframe["receptor_latitude"] = float(
                        configuration["latitude"]
                    )

                    dataframe["receptor_longitude"] = float(
                        configuration["longitude"]
                    )

                    trajectory_frames.append(dataframe)

                    run_summary["parsed"] = True
                    run_summary["number_of_points"] = len(
                        dataframe
                    )

                except Exception as error:
                    run_summary["status"] = "parse_failed"
                    run_summary["parsed"] = False
                    run_summary["parse_error"] = str(error)
                    run_summary["number_of_points"] = 0

            else:
                run_summary["parsed"] = False
                run_summary["number_of_points"] = 0

            run_results.append(run_summary)

    if trajectory_frames:
        trajectories = pd.concat(
            trajectory_frames,
            ignore_index=True,
        )
    else:
        trajectories = pd.DataFrame()

    successful_runs = sum(
        result["status"] == "complete"
        and result["parsed"]
        for result in run_results
    )

    failed_runs = len(run_results) - successful_runs

    return {
        "status": (
            "complete"
            if failed_runs == 0
            else "partial_failure"
        ),
        "run_results": run_results,
        "trajectories": trajectories,
        "number_of_runs": len(run_results),
        "successful_runs": successful_runs,
        "failed_runs": failed_runs,
        "number_of_points": len(trajectories),
    }


def _safe_filename(value):
    value = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        str(value).strip(),
    )
    return value.strip("_") or "receptor"


def save_batch_results(
    batch_result,
    configuration,
    results_root=DEFAULT_RESULTS_DIRECTORY,
):
    """
    Save combined trajectory points, run summaries and metadata.

    A batch with a "partial_failure" status is still saved as long as at
    least one run completed and parsed successfully: the failed runs are
    recorded in run_summary.csv and reflected in metadata.json's
    failed_runs count, rather than causing every successfully-computed
    trajectory in the batch to be discarded because one run failed.
    """
    _require_configuration(configuration)

    if not isinstance(batch_result, dict):
        raise TypeError(
            "batch_result must be the dict returned by "
            f"run_trajectory_batch(), got {type(batch_result).__name__}."
        )

    if batch_result.get("successful_runs", 0) == 0:
        raise RuntimeError(
            "Results cannot be finalized: no HYSPLIT runs in this batch "
            "completed and parsed successfully."
        )

    trajectories = batch_result["trajectories"].copy()

    if trajectories.empty:
        raise RuntimeError(
            "Results cannot be saved because the trajectory "
            "DataFrame is empty."
        )

    receptor_text = (
        configuration.get("lake_id")
        or configuration["receptor_name"]
    )

    receptor_slug = _safe_filename(receptor_text)

    meteorology_dataset = configuration["meteorology"]
    meteorology_slug = _safe_filename(meteorology_dataset)
    meteorology_resolution_degrees = {
        "GDAS1": 1.0,
        "GFS0P25": 0.25,
    }.get(meteorology_dataset)

    trajectories["meteorology_dataset"] = meteorology_dataset
    trajectories["meteorology_resolution_degrees"] = (
        meteorology_resolution_degrees
    )

    earliest_arrival = min(
        configuration["arrival_datetimes"]
    )

    latest_arrival = max(
        configuration["arrival_datetimes"]
    )

    package_name = (
        f"{receptor_slug}_"
        f"{meteorology_slug}_"
        f"{earliest_arrival:%Y%m%d_%H%M}_"
        f"{latest_arrival:%Y%m%d_%H%M}"
    )

    output_directory = Path(results_root) / package_name
    output_directory.mkdir(parents=True, exist_ok=True)

    trajectory_csv = (
        output_directory / "trajectory_points.csv"
    )

    run_summary_csv = (
        output_directory / "run_summary.csv"
    )

    metadata_json = (
        output_directory / "metadata.json"
    )

    trajectories.to_csv(
        trajectory_csv,
        index=False,
    )

    run_summary_records = []

    for result in batch_result["run_results"]:
        run_summary_records.append(
            {
                "run_name": result["run_name"],
                "meteorology_dataset": meteorology_dataset,
                "meteorology_resolution_degrees": (
                    meteorology_resolution_degrees
                ),
                "arrival_datetime": (
                    result["arrival_datetime"].isoformat()
                ),
                "arrival_height_m_agl": (
                    result["height_m_agl"]
                ),
                "status": result["status"],
                "return_code": result["return_code"],
                "elapsed_seconds": result["elapsed_seconds"],
                "parsed": result["parsed"],
                "number_of_points": (
                    result["number_of_points"]
                ),
                "tdump_size_bytes": (
                    result["output_size_bytes"]
                ),
            }
        )

    pd.DataFrame(run_summary_records).to_csv(
        run_summary_csv,
        index=False,
    )

    metadata = {
        "receptor_name": configuration["receptor_name"],
        "lake_id": configuration.get("lake_id"),
        "latitude": configuration["latitude"],
        "longitude": configuration["longitude"],
        "direction": configuration["direction"],
        "backward_duration_hours": (
            configuration["backward_duration_hours"]
        ),
        "hysplit_duration": (
            configuration["hysplit_duration"]
        ),
        "arrival_times_utc": [
            value.isoformat()
            for value in configuration["arrival_datetimes"]
        ],
        "heights_m_agl": (
            configuration["heights_m_agl"]
        ),
        "vertical_motion_name": (
            configuration["vertical_motion_name"]
        ),
        "vertical_motion_code": (
            configuration["vertical_motion_code"]
        ),
        "model_top_metres": (
            configuration["model_top_metres"]
        ),
        # Keep the original key for compatibility with previous releases.
        "meteorology": meteorology_dataset,
        "meteorology_dataset": meteorology_dataset,
        "meteorology_resolution_degrees": (
            meteorology_resolution_degrees
        ),
        "meteorology_files": [
            item["filename"]
            for item in meteorology_inputs(configuration)[0]["files"]
        ],
        "number_of_runs": batch_result["number_of_runs"],
        "successful_runs": batch_result["successful_runs"],
        "failed_runs": batch_result["failed_runs"],
        "number_of_points": batch_result["number_of_points"],
    }

    metadata_json.write_text(
        json.dumps(
            metadata,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return {
        "output_directory": output_directory,
        "trajectory_csv": trajectory_csv,
        "run_summary_csv": run_summary_csv,
        "metadata_json": metadata_json,
        "number_of_points": len(trajectories),
    }

class HysplitEnvironmentError(RuntimeError):
    """Raised when HYSPLIT cannot safely start."""


def validate_hysplit_environment(
    meteorology_plan=None,
    executable=DEFAULT_HYSPLIT_EXECUTABLE,
    ascdata_file=DEFAULT_ASCDATA_FILE,
    meteorology_directory=DEFAULT_GDAS1_DIRECTORY,
):
    """
    Validate the HYSPLIT executable, ASCDATA file and required
    meteorological files.
    """
    executable = Path(executable)
    ascdata_file = Path(ascdata_file)
    meteorology_directory = Path(meteorology_directory)

    errors = []

    if not executable.is_file():
        errors.append(
            f"HYSPLIT executable was not found: {executable}"
        )
    elif not os.access(executable, os.X_OK):
        errors.append(
            f"HYSPLIT executable permission is missing: {executable}"
        )

    if not ascdata_file.is_file():
        errors.append(
            f"ASCDATA.CFG was not found: {ascdata_file}"
        )

    meteorology_files = []

    if meteorology_plan is not None:
        if not isinstance(meteorology_plan, dict) or not isinstance(
            meteorology_plan.get("files"), list
        ):
            errors.append(
                "meteorology_plan must be a dict with a 'files' list "
                f"(or None to skip meteorology checks), got "
                f"{meteorology_plan!r}"
            )
            meteorology_plan = None

    if meteorology_plan is not None:
        for file_info in meteorology_plan["files"]:
            path = meteorology_directory / file_info["filename"]
            exists = path.is_file()
            size_bytes = path.stat().st_size if exists else 0
            ready = exists and size_bytes > 0

            meteorology_files.append(
                {
                    "filename": file_info["filename"],
                    "path": path,
                    "exists": exists,
                    "size_bytes": size_bytes,
                    "ready": ready,
                }
            )

            if not ready:
                errors.append(
                    f"Required meteorological file is missing "
                    f"or empty: {path}"
                )

    return {
        "ready": not errors,
        "errors": errors,
        "executable": executable,
        "ascdata_file": ascdata_file,
        "meteorology_directory": meteorology_directory,
        "meteorology_files": meteorology_files,
    }


def require_hysplit_environment(
    meteorology_plan=None,
    **kwargs,
):
    """
    Validate the environment and raise a readable exception when
    HYSPLIT is not ready.
    """
    status = validate_hysplit_environment(
        meteorology_plan=meteorology_plan,
        **kwargs,
    )

    if not status["ready"]:
        error_text = "\n".join(
            f"- {message}"
            for message in status["errors"]
        )

        raise HysplitEnvironmentError(
            "HYSPLIT environment validation failed:\n"
            f"{error_text}"
        )

    return status

