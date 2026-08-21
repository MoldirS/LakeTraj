from pathlib import Path

import pandas as pd


class TdumpParseError(RuntimeError):
    """Raised when a HYSPLIT tdump file cannot be parsed."""


def _four_digit_year(two_digit_year):
    two_digit_year = int(two_digit_year)

    if two_digit_year <= 49:
        return 2000 + two_digit_year

    return 1900 + two_digit_year


def parse_tdump(path):
    """
    Parse a HYSPLIT trajectory tdump file into a pandas DataFrame.
    """
    path = Path(path)

    if not path.is_file():
        raise FileNotFoundError(f"tdump file not found: {path}")

    lines = [
        line.strip()
        for line in path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
        if line.strip()
    ]

    if not lines:
        raise TdumpParseError(f"tdump file is empty: {path}")

    try:
        # Meteorological-file header
        first_tokens = lines[0].split()
        number_of_meteorology_records = int(first_tokens[0])

        index = 1 + number_of_meteorology_records

        # Trajectory header
        trajectory_header = lines[index].split()
        number_of_trajectories = int(trajectory_header[0])
        direction = trajectory_header[1]
        vertical_motion = " ".join(trajectory_header[2:])
        index += 1

        # Starting-location records
        starting_locations = []

        for _ in range(number_of_trajectories):
            tokens = lines[index].split()

            starting_locations.append(
                {
                    "year": _four_digit_year(tokens[0]),
                    "month": int(tokens[1]),
                    "day": int(tokens[2]),
                    "hour": int(tokens[3]),
                    "latitude": float(tokens[4]),
                    "longitude": float(tokens[5]),
                    "height_m_agl": float(tokens[6]),
                }
            )

            index += 1

        # Diagnostic-variable header
        diagnostic_header = lines[index].split()
        number_of_diagnostics = int(diagnostic_header[0])

        if len(diagnostic_header) < 1 + number_of_diagnostics:
            # Header claims more diagnostic variables than it actually
            # names. Slicing would silently drop the missing ones instead
            # of raising, so the rest of the file would parse "successfully"
            # with fewer diagnostic columns than the data rows contain.
            raise TdumpParseError(
                f"Invalid tdump header in {path}: diagnostic header at "
                f"line {index + 1} declares {number_of_diagnostics} "
                f"variable(s) but only lists "
                f"{len(diagnostic_header) - 1} name(s): {diagnostic_header!r}"
            )

        diagnostic_names = [
            name.lower()
            for name in diagnostic_header[
                1:1 + number_of_diagnostics
            ]
        ]
        index += 1

    except (IndexError, TypeError, ValueError) as error:
        raise TdumpParseError(
            f"Invalid tdump header in {path}: {error}"
        ) from error

    records = []

    for line_number, line in enumerate(
        lines[index:],
        start=index + 1,
    ):
        tokens = line.split()

        minimum_columns = 12 + number_of_diagnostics

        if len(tokens) < minimum_columns:
            raise TdumpParseError(
                f"Invalid trajectory record at line {line_number}: "
                f"expected at least {minimum_columns} columns, "
                f"found {len(tokens)}."
            )

        try:
            timestamp = pd.Timestamp(
                year=_four_digit_year(tokens[2]),
                month=int(tokens[3]),
                day=int(tokens[4]),
                hour=int(tokens[5]),
                minute=int(tokens[6]),
                tz="UTC",
            )

            record = {
                "trajectory_number": int(tokens[0]),
                "meteorology_grid": int(tokens[1]),
                "timestamp_utc": timestamp,
                "forecast_hour": int(tokens[7]),
                "age_hours": float(tokens[8]),
                "latitude": float(tokens[9]),
                "longitude": float(tokens[10]),
                "height_m_agl": float(tokens[11]),
            }

            for diagnostic_index, diagnostic_name in enumerate(
                diagnostic_names
            ):
                record[diagnostic_name] = float(
                    tokens[12 + diagnostic_index]
                )

            records.append(record)

        except (TypeError, ValueError) as error:
            raise TdumpParseError(
                f"Invalid trajectory values at line "
                f"{line_number}: {error}"
            ) from error

    if not records:
        raise TdumpParseError(
            f"No trajectory records were found in {path}."
        )

    dataframe = pd.DataFrame.from_records(records)

    dataframe.attrs["source_path"] = str(path)
    dataframe.attrs["direction"] = direction
    dataframe.attrs["vertical_motion"] = vertical_motion
    dataframe.attrs["number_of_trajectories"] = (
        number_of_trajectories
    )
    dataframe.attrs["starting_locations"] = starting_locations
    dataframe.attrs["diagnostic_names"] = diagnostic_names

    return dataframe

