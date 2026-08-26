import html
import base64
import os
from pathlib import Path
import ipyleaflet
import pandas as pd
import solara
from ipywidgets import HTML, Layout
from shapely.geometry import Point, shape

# CARTO now requires an API key on every basemap tile request (see
# https://carto.com/basemaps/apikey). Reading it from an environment
# variable keeps it out of source control instead of hardcoded here -
# note that the key still travels to the browser inside the tile URL,
# so it isn't hidden from end users, only from the repo/source code.
CARTO_API_KEY = os.environ.get("CARTO_API_KEY", "")


def _carto_tile_layer():
    """Build the shared CARTO Positron basemap layer used on every map."""
    url = "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"
    if CARTO_API_KEY:
        url += f"?key={CARTO_API_KEY}"

    return ipyleaflet.TileLayer.element(
        url=url,
        attribution=(
            '&copy; <a href="https://www.openstreetmap.org/copyright">'
            "OpenStreetMap contributors</a> "
            '&copy; <a href="https://carto.com/attributions">CARTO</a>'
        ),
        name="CARTO Positron",
        max_zoom=20,
    )


def _display_geometry(geometry_geojson, latitude, longitude):
    """For a MultiPolygon, select the polygon containing the receptor."""
    if geometry_geojson is None:
        return None

    geometry = shape(geometry_geojson)
    receptor = Point(longitude, latitude)

    if geometry.geom_type == "MultiPolygon":
        containing_parts = [
            polygon for polygon in geometry.geoms
            if polygon.covers(receptor)
        ]

        if containing_parts:
            return max(containing_parts, key=lambda polygon: polygon.area)

        return max(geometry.geoms, key=lambda polygon: polygon.area)

    return geometry


def _view_settings(geometry, latitude, longitude):
    """Calculate a suitable centre and zoom without calling map.fit_bounds()."""
    if geometry is None:
        return (latitude, longitude), 6

    min_lon, min_lat, max_lon, max_lat = geometry.bounds

    centre = (
        (min_lat + max_lat) / 2,
        (min_lon + max_lon) / 2,
    )

    span = max(max_lon - min_lon, max_lat - min_lat)

    if span < 0.03:
        zoom = 12
    elif span < 0.08:
        zoom = 10
    elif span < 0.20:
        zoom = 9
    elif span < 0.50:
        zoom = 8
    elif span < 1.5:
        zoom = 7
    elif span < 4:
        zoom = 6
    elif span < 10:
        zoom = 5
    else:
        zoom = 4

    return centre, zoom


def _arrival_labels(values):
    """Return compact, consistently formatted UTC arrival labels."""
    parsed = pd.to_datetime(values, utc=True, errors="coerce")
    labels = parsed.dt.strftime("%Y-%m-%d %H:%M UTC")
    return labels.fillna(values.astype(str))


def trajectory_filter_options(trajectory_csv_path):
    """Return available arrival labels and heights from a result CSV."""
    if not trajectory_csv_path:
        return [], []

    path = Path(trajectory_csv_path)
    if not path.is_file() or path.stat().st_size == 0:
        return [], []

    try:
        data = pd.read_csv(
            path,
            usecols=["arrival_datetime_utc", "arrival_height_m_agl"],
        )
    except (OSError, ValueError, pd.errors.EmptyDataError):
        return [], []

    arrival_options = sorted(
        _arrival_labels(data["arrival_datetime_utc"])
        .dropna()
        .unique()
        .tolist()
    )
    height_options = sorted(
        pd.to_numeric(data["arrival_height_m_agl"], errors="coerce")
        .dropna()
        .astype(int)
        .unique()
        .tolist()
    )
    return arrival_options, height_options


@solara.component
def EmptyReceptorMapElement():
    """Show an empty Europe-centred map before a receptor is selected."""
    tile_layer = _carto_tile_layer()

    ipyleaflet.Map.element(
        center=(50.0, 10.0),
        zoom=4,
        layers=[tile_layer],
        scroll_wheel_zoom=True,
        layout=Layout(
            width="100%",
            height="600px",
        ),
    )


@solara.component
def ManualReceptorMapElement(
    latitude,
    longitude,
    on_location=None,
    locked=False,
):
    """Preview and edit a manual receptor with a draggable marker."""
    latitude = float(latitude)
    longitude = float(longitude)

    tile_layer = _carto_tile_layer()

    coordinate_popup = HTML(
        value=(
            "<b>Manual receptor preview</b><br>"
            f"Latitude: {latitude:.6f}<br>"
            f"Longitude: {longitude:.6f}<br>"
            "Drag this marker or edit the coordinate fields."
        )
    )

    marker = ipyleaflet.Marker.element(
        location=(latitude, longitude),
        draggable=not locked,
        on_location=on_location,
        popup=coordinate_popup,
        title="Manual receptor preview",
    )

    ipyleaflet.Map.element(
        center=(latitude, longitude),
        zoom=6,
        layers=[tile_layer, marker],
        scroll_wheel_zoom=True,
        layout=Layout(width="100%", height="650px"),
    )


@solara.component
def LakeSelectionMapElement(
    visible_lakes=None,
    candidate=None,
    center=(50.0, 10.0),
    zoom=4,
    on_center=None,
    on_zoom=None,
    on_bounds=None,
    on_lake_click=None,
):
    """Map used to query and confirm one LakeCCI receptor candidate."""
    tile_layer = _carto_tile_layer()
    layers = [tile_layer]
    centre = tuple(float(value) for value in center)
    zoom = int(zoom)
    visible_lakes = visible_lakes or []

    if visible_lakes:
        layers.append(
            ipyleaflet.GeoJSON.element(
                data={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"lake_id": lake["lake_id"]},
                            "geometry": lake["geometry_geojson"],
                        }
                        for lake in visible_lakes
                    ],
                },
                name="LakeCCI polygons in current view",
                style={
                    "color": "#0072B2",
                    "weight": 2,
                    "opacity": 0.9,
                    "fillColor": "#56B4E9",
                    "fillOpacity": 0.12,
                },
                hover_style={
                    "color": "#004C73",
                    "weight": 4,
                    "fillOpacity": 0.25,
                },
                on_click=on_lake_click,
            )
        )

    if candidate is not None:
        latitude = float(candidate["latitude"])
        longitude = float(candidate["longitude"])
        geometry = _display_geometry(
            candidate.get("geometry_geojson"),
            latitude,
            longitude,
        )
        if geometry is not None:
            layers.append(
                ipyleaflet.GeoJSON.element(
                    data=geometry.__geo_interface__,
                    name=f'{candidate["lake_id"]} candidate',
                    style={
                        "color": "#D55E00",
                        "weight": 4,
                        "opacity": 1.0,
                        "fillColor": "#E69F00",
                        "fillOpacity": 0.28,
                    },
                )
            )
        layers.append(
            ipyleaflet.CircleMarker.element(
                location=(latitude, longitude),
                radius=6,
                color="#111827",
                weight=2,
                fill=True,
                fill_color="#E69F00",
                fill_opacity=1.0,
            )
        )

    ipyleaflet.Map.element(
        center=centre,
        zoom=zoom,
        layers=layers,
        scroll_wheel_zoom=True,
        on_center=on_center,
        on_zoom=on_zoom,
        on_bounds=on_bounds,
        layout=Layout(width="100%", height="650px"),
    )


@solara.component
def ReceptorMapElement(
    name,
    latitude,
    longitude,
    lake_id=None,
    method="manual coordinates",
    geometry_geojson=None,
    trajectory_csv_path=None,
    selected_arrival_times=None,
    selected_heights_m_agl=None,
):
    latitude = float(latitude)
    longitude = float(longitude)

    geometry = _display_geometry(
        geometry_geojson,
        latitude,
        longitude,
    )

    centre, zoom = _view_settings(
        geometry,
        latitude,
        longitude,
    )

    trajectory_data = None
    if trajectory_csv_path:
        trajectory_path = Path(trajectory_csv_path)
        if trajectory_path.is_file() and trajectory_path.stat().st_size > 0:
            candidate_data = pd.read_csv(trajectory_path)
            required_columns = {
                "latitude",
                "longitude",
                "arrival_datetime_utc",
                "arrival_height_m_agl",
            }
            if required_columns.issubset(candidate_data.columns):
                candidate_data = candidate_data.dropna(
                    subset=["latitude", "longitude"]
                )
                candidate_data["_arrival_label"] = _arrival_labels(
                    candidate_data["arrival_datetime_utc"]
                )
                candidate_data["arrival_height_m_agl"] = pd.to_numeric(
                    candidate_data["arrival_height_m_agl"],
                    errors="coerce",
                )
                if selected_arrival_times is not None:
                    candidate_data = candidate_data[
                        candidate_data["_arrival_label"].isin(
                            selected_arrival_times
                        )
                    ]
                if selected_heights_m_agl is not None:
                    candidate_data = candidate_data[
                        candidate_data["arrival_height_m_agl"].isin(
                            selected_heights_m_agl
                        )
                    ]
                if not candidate_data.empty:
                    trajectory_data = candidate_data

    if trajectory_data is not None:
        latitudes = [latitude] + trajectory_data["latitude"].astype(float).tolist()
        longitudes = [longitude] + trajectory_data["longitude"].astype(float).tolist()

        if geometry is not None:
            min_lon, min_lat, max_lon, max_lat = geometry.bounds
            latitudes.extend([min_lat, max_lat])
            longitudes.extend([min_lon, max_lon])

        min_latitude = min(latitudes)
        max_latitude = max(latitudes)
        min_longitude = min(longitudes)
        max_longitude = max(longitudes)
        centre = (
            (min_latitude + max_latitude) / 2,
            (min_longitude + max_longitude) / 2,
        )
        span = max(
            max_latitude - min_latitude,
            max_longitude - min_longitude,
        )
        if span < 0.20:
            zoom = 9
        elif span < 0.50:
            zoom = 8
        elif span < 1.5:
            zoom = 7
        elif span < 4:
            zoom = 6
        elif span < 10:
            zoom = 5
        elif span < 25:
            zoom = 4
        elif span < 55:
            zoom = 3
        else:
            zoom = 2

    tile_layer = _carto_tile_layer()

    # Keep the permanent map layers in a stable order. Trajectory layers
    # are collected separately and appended only after the lake and
    # receptor, preventing the existing map from being reconstructed.
    layers = [tile_layer]
    trajectory_layers = []

    height_colours = {
        500: "#1b9e77",
        1000: "#d95f02",
        1500: "#7570b3",
    }
    show_age_labels = (
        selected_arrival_times is None
        or len(selected_arrival_times) == 1
    )

    if trajectory_data is not None:
        group_columns = [
            "_arrival_label",
            "arrival_height_m_agl",
        ]
        for (arrival_time, height), group in trajectory_data.groupby(
            group_columns,
            sort=True,
        ):
            if "age_hours" in group.columns:
                group = group.sort_values("age_hours", ascending=False)

            locations = [
                (float(row.latitude), float(row.longitude))
                for row in group.itertuples()
            ]
            if len(locations) < 2:
                continue

            height = int(height)
            trajectory_layer = ipyleaflet.Polyline.element(
                locations=locations,
                color=height_colours.get(height, "#0072B2"),
                weight=3,
                opacity=0.90,
                fill=False,
                name=f"{arrival_time} — {height} m AGL",
            )
            trajectory_layers.append(trajectory_layer)

            # Add reference-style time markers along each trajectory.
            # Small hollow circles indicate 6-hour endpoints. Larger
            # circles and labels mark complete 24-hour intervals.
            if "age_hours" in group.columns:
                marker_rows = group[
                    (group["age_hours"] < 0)
                    & (
                        group["age_hours"]
                        .round()
                        .astype(int)
                        .abs()
                        .mod(6)
                        == 0
                    )
                ]

                for endpoint in marker_rows.itertuples():
                    age_hours = int(round(float(endpoint.age_hours)))
                    is_daily = abs(age_hours) % 24 == 0
                    colour = height_colours.get(height, "#0072B2")
                    endpoint_time = getattr(
                        endpoint,
                        "timestamp_utc",
                        "not available",
                    )
                    endpoint_height = getattr(
                        endpoint,
                        "height_m_agl",
                        None,
                    )
                    endpoint_height_text = (
                        f"{float(endpoint_height):.0f} m AGL"
                        if endpoint_height is not None
                        else "not available"
                    )

                    endpoint_popup = HTML.element(
                        value=f"""
                        <div style="
                            min-width:220px;
                            padding:6px 8px;
                            color:#202124 !important;
                            background:#ffffff !important;
                            font-family:Arial, Helvetica, sans-serif;
                            font-size:13px;
                            line-height:1.45;
                        ">
                            <b>Arrival:</b> {html.escape(str(arrival_time))}<br>
                            <b>Arrival height:</b> {height} m AGL<br>
                            <b>Trajectory age:</b> {age_hours} h<br>
                            <b>Endpoint time:</b> {html.escape(str(endpoint_time))}<br>
                            <b>Endpoint height:</b> {endpoint_height_text}
                        </div>
                        """
                    )
                    endpoint_marker = ipyleaflet.CircleMarker.element(
                        location=(
                            float(endpoint.latitude),
                            float(endpoint.longitude),
                        ),
                        # ipyleaflet CircleMarker.radius is an Int trait.
                        radius=5 if is_daily else 3,
                        color=colour,
                        # ipyleaflet CircleMarker.weight is also Int.
                        weight=2 if is_daily else 1,
                        opacity=1.0,
                        fill=True,
                        fill_color="white",
                        fill_opacity=1.0,
                        popup=endpoint_popup,
                    )
                    trajectory_layers.append(endpoint_marker)

                    if is_daily and show_age_labels:
                        # Use a transparent SVG icon instead of DivIcon.
                        # This avoids Leaflet's default bordered label box.
                        label_svg = f"""
                        <svg xmlns="http://www.w3.org/2000/svg"
                             width="54" height="18" viewBox="0 0 54 18">
                          <text x="2" y="13"
                                fill="{colour}"
                                stroke="#ffffff"
                                stroke-width="3"
                                paint-order="stroke"
                                font-family="Arial, Helvetica, sans-serif"
                                font-size="11"
                                font-weight="700">{age_hours} h</text>
                        </svg>
                        """
                        label_data_url = (
                            "data:image/svg+xml;base64,"
                            + base64.b64encode(
                                label_svg.encode("utf-8")
                            ).decode("ascii")
                        )
                        label_icon = ipyleaflet.Icon.element(
                            icon_url=label_data_url,
                            icon_size=[54, 16],
                            icon_anchor=[-7, 8],
                        )
                        age_label = ipyleaflet.Marker.element(
                            location=(
                                float(endpoint.latitude),
                                float(endpoint.longitude),
                            ),
                            icon=label_icon,
                            draggable=False,
                        )
                        trajectory_layers.append(age_label)

    if geometry is not None:
        lake_layer = ipyleaflet.GeoJSON.element(
            data=geometry.__geo_interface__,
            name=f"{lake_id} boundary",
            style={
                "color": "#0072B2",
                "weight": 3,
                "opacity": 0.9,
                "fillColor": "#56B4E9",
                "fillOpacity": 0.18,
            },
            hover_style={
                "color": "#004C73",
                "weight": 4,
                "fillColor": "#56B4E9",
                "fillOpacity": 0.28,
            },
        )
        layers.append(lake_layer)

    lake_id_text = (
        html.escape(str(lake_id))
        if lake_id
        else "Not associated with LakeCCI"
    )

    popup = HTML.element(
        value=f"""
        <div style="
            min-width: 245px;
            padding: 6px 8px;
            color: #202124 !important;
            background: #ffffff;
            font-family: Arial, Helvetica, sans-serif;
            font-size: 13px;
            line-height: 1.45;
        ">
            <div style="
                margin-bottom: 7px;
                color: #111827 !important;
                font-size: 15px;
                font-weight: 700;
            ">
                {html.escape(str(name))}
            </div>

            <table style="
                width: 100%;
                border-collapse: collapse;
                color: #202124 !important;
            ">
                <tr>
                    <td style="padding: 2px 8px 2px 0; font-weight: 600;">
                        LakeCCI ID
                    </td>
                    <td style="padding: 2px 0;">
                        {lake_id_text}
                    </td>
                </tr>

                <tr>
                    <td style="padding: 2px 8px 2px 0; font-weight: 600;">
                        Latitude
                    </td>
                    <td style="padding: 2px 0;">
                        {latitude:.6f}
                    </td>
                </tr>

                <tr>
                    <td style="padding: 2px 8px 2px 0; font-weight: 600;">
                        Longitude
                    </td>
                    <td style="padding: 2px 0;">
                        {longitude:.6f}
                    </td>
                </tr>

                <tr>
                    <td style="padding: 2px 8px 2px 0; font-weight: 600;">
                        Method
                    </td>
                    <td style="padding: 2px 0;">
                        {html.escape(str(method))}
                    </td>
                </tr>
            </table>
        </div>
        """

    )

    marker_svg = """
    <svg
        xmlns="http://www.w3.org/2000/svg"
        width="30"
        height="40"
        viewBox="0 0 44 56"
    >
        <defs>
            <filter
                id="shadow"
                x="-30%"
                y="-20%"
                width="160%"
                height="160%"
            >
                <feDropShadow
                    dx="0"
                    dy="2"
                    stdDeviation="1.5"
                    flood-color="#000000"
                    flood-opacity="0.35"
                />
            </filter>
        </defs>

        <path
            d="
                M22 1
                C10.4 1 1 10.4 1 22
                C1 37 22 55 22 55
                C22 55 43 37 43 22
                C43 10.4 33.6 1 22 1
                Z
            "
            fill="#252525"
            stroke="#111111"
            stroke-width="1.5"
            filter="url(#shadow)"
        />

        <path
            d="
                M22 10
                L25.4 17
                L33 18.1
                L27.5 23.5
                L28.8 31
                L22 27.4
                L15.2 31
                L16.5 23.5
                L11 18.1
                L18.6 17
                Z
            "
            fill="#ffffff"
        />
    </svg>
    """

    marker_data_url = (
        "data:image/svg+xml;base64,"
        + base64.b64encode(marker_svg.encode("utf-8")).decode("ascii")
    )

    receptor_icon = ipyleaflet.Icon.element(
        icon_url=marker_data_url,
        icon_size=[30, 40],
        icon_anchor=[15, 39],
        popup_anchor=[0, -38],
    )

    receptor_marker = ipyleaflet.Marker.element(
        location=(latitude, longitude),
        icon=receptor_icon,
        popup=popup,
        title=str(name),
        draggable=False,
    )


    layers.append(receptor_marker)
    layers.extend(trajectory_layers)

    ipyleaflet.Map.element(
        center=centre,
        zoom=zoom,
        layers=layers,
        scroll_wheel_zoom=True,
        layout=Layout(
            width="100%",
            height="650px",
        ),
    )