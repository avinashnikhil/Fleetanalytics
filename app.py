# =====================================================================
# app.py : Autonomous Delivery Fleet Analytics (Streamlit dashboard)
#
# What this file does:
#   1. Opens the DuckDB database fleet.duckdb (read-only).
#   2. Lets the visitor filter by date, zone, software version, weather.
#   3. Shows four KPI tiles and charts, all built from the SQL views.
#
# It must sit in the root of the GitHub repo, next to fleet.duckdb and
# requirements.txt. Streamlit Community Cloud runs it from there.
# =====================================================================

from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

# ---------------------------------------------------------------------
# SETTINGS (all in CAPITAL LETTERS, change them here and nowhere else)
# ---------------------------------------------------------------------
# The database path is built from this file's own folder, so it works
# both on my computer and on Streamlit Community Cloud.
DATABASE_FILE = str(Path(__file__).parent / "fleet.duckdb")

AUTHOR_NAME = "Avinash Nikhil Chitra"   # shown in the footer

CHART_TEMPLATE = "plotly_white"         # clean white chart style
CHART_MAIN_COLOR = "#2F6DB5"            # calm blue for normal bars and lines
CHART_HIGHLIGHT_COLOR = "#E8832A"       # orange to pick out the "bad" item
CHART_GREY_COLOR = "#B8BEC6"            # grey for everything that is not the focus
CHART_HEIGHT = 420                      # chart height in pixels

V22_START_DATE = "2026-07-31"           # day 31: v2.2 starts for every robot
V23_START_DATE = "2026-08-30"           # day 61: v2.3 starts on half the fleet

# The columns in v_daily_slice that can safely be added together.
# Every KPI is built from these sums (ratio of sums, never average of ratios).
ADDITIVE_COLUMNS = [
    "missions",
    "completed_deliveries",
    "zero_intervention_missions",
    "interventions",
    "km",
    "on_time_deliveries",
    "total_cost",
]

# This must be the first Streamlit command in the file
st.set_page_config(page_title="Robot Fleet Analytics", layout="wide")


# =====================================================================
# DATABASE HELPERS
# =====================================================================
@st.cache_resource
def get_database_connection():
    """Open the database once and reuse it for every visitor."""
    # read_only=True is needed because many visitors share one file at the
    # same time. A read-only connection can never change or lock the data.
    return duckdb.connect(DATABASE_FILE, read_only=True)


@st.cache_data
def run_query(sql_text, query_parameters=None):
    """Run one SQL query and return a pandas table (results are cached)."""
    # A cursor is a lightweight copy of the connection. Streamlit handles
    # several visitors at once, and each query should use its own cursor.
    database_cursor = get_database_connection().cursor()

    # The values travel separately from the SQL text (the ? placeholders),
    # so nobody can sneak SQL commands in through a filter box.
    if query_parameters is None:
        return database_cursor.execute(sql_text).df()
    return database_cursor.execute(sql_text, query_parameters).df()


def load_slice(first_date, last_date, zones, versions, weathers):
    """Return the rows of v_daily_slice that match the chosen filters."""
    slice_query = """
        SELECT *
        FROM v_daily_slice
        WHERE start_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
          AND list_contains(CAST(? AS VARCHAR[]), zone)
          AND list_contains(CAST(? AS VARCHAR[]), software_version)
          AND list_contains(CAST(? AS VARCHAR[]), weather)
    """
    # Values for the five ? marks, in the same order as in the query
    query_parameters = [
        str(first_date),
        str(last_date),
        list(zones),
        list(versions),
        list(weathers),
    ]
    return run_query(slice_query, query_parameters)


# =====================================================================
# KPI HELPERS
# =====================================================================
def calculate_kpis(slice_dataframe):
    """Return the four KPIs for the rows given, as ratios of sums."""
    # Add up each column once across all rows
    total_missions = float(slice_dataframe["missions"].sum())
    total_zero_intervention_missions = float(
        slice_dataframe["zero_intervention_missions"].sum()
    )
    total_interventions = float(slice_dataframe["interventions"].sum())
    total_km = float(slice_dataframe["km"].sum())
    total_completed_deliveries = float(slice_dataframe["completed_deliveries"].sum())
    total_on_time_deliveries = float(slice_dataframe["on_time_deliveries"].sum())
    total_cost = float(slice_dataframe["total_cost"].sum())

    # Each KPI is None when its denominator is zero (nothing to divide by)
    autonomy_rate = None
    if total_missions > 0:
        autonomy_rate = total_zero_intervention_missions / total_missions

    interventions_per_1000km = None
    if total_km > 0:
        interventions_per_1000km = total_interventions / total_km * 1000

    on_time_pct = None
    if total_completed_deliveries > 0:
        on_time_pct = total_on_time_deliveries / total_completed_deliveries

    cost_per_delivery = None
    if total_completed_deliveries > 0:
        cost_per_delivery = total_cost / total_completed_deliveries

    return {
        "autonomy_rate": autonomy_rate,
        "interventions_per_1000km": interventions_per_1000km,
        "on_time_pct": on_time_pct,
        "cost_per_delivery": cost_per_delivery,
    }


def summarise_slice(slice_dataframe, group_column):
    """One row per value of group_column, with sums and the four KPIs.

    Column names match my SQL views (v_by_version, v_by_zone, ...).
    """
    # Step 1: add up the additive columns inside each group
    summary_table = slice_dataframe.groupby(group_column, as_index=False)[
        ADDITIVE_COLUMNS
    ].sum()

    # Step 2: divide SUM by SUM. We never average the daily ratios: a quiet
    # day with 2 missions would count as much as a busy day with 200.
    # .where(... > 0) turns a zero denominator into "missing" (NaN),
    # so the division never crashes.
    safe_missions = summary_table["missions"].where(summary_table["missions"] > 0)
    safe_km = summary_table["km"].where(summary_table["km"] > 0)
    safe_completed = summary_table["completed_deliveries"].where(
        summary_table["completed_deliveries"] > 0
    )

    summary_table["autonomy_rate"] = (
        summary_table["zero_intervention_missions"] / safe_missions
    )
    summary_table["interventions_per_1000km"] = (
        summary_table["interventions"] / safe_km * 1000
    )
    summary_table["on_time_pct"] = summary_table["on_time_deliveries"] / safe_completed
    summary_table["cost_per_delivery"] = summary_table["total_cost"] / safe_completed

    return summary_table


def format_percent(value):
    """Show a 0-1 share as a percentage, or a dash when there is no value."""
    if value is None or pd.isna(value):
        return "-"
    return f"{value * 100:.1f}%"


def format_number(value):
    """Show a number with 1 decimal, or a dash when there is no value."""
    if value is None or pd.isna(value):
        return "-"
    return f"{value:.1f}"


def format_dollars(value):
    """Show a number as dollars, or a dash when there is no value."""
    if value is None or pd.isna(value):
        return "-"
    return f"${value:.2f}"


def build_delta_text(current_value, previous_value, delta_kind):
    """Text for the small arrow under a KPI tile, or None if not possible."""
    if (
        current_value is None
        or previous_value is None
        or pd.isna(current_value)
        or pd.isna(previous_value)
    ):
        return None

    difference = current_value - previous_value
    if delta_kind == "percent":
        return f"{difference * 100:+.1f} pts"   # share: show percentage points
    if delta_kind == "dollars":
        return f"{difference:+.2f} $"
    return f"{difference:+.1f}"


# =====================================================================
# CHART FUNCTIONS (from notebook cells E2 to E5)
# =====================================================================
def make_daily_trend_chart(daily_dataframe):
    """Line chart of interventions per 1,000 km, daily and 7-day average."""
    # Make sure dates are real dates and rows are in time order
    chart_data = daily_dataframe.copy()
    chart_data["start_date"] = pd.to_datetime(chart_data["start_date"])
    chart_data = chart_data.sort_values("start_date")

    trend_figure = go.Figure()

    # Thin light line: the daily value (noisy)
    trend_figure.add_trace(
        go.Scatter(
            x=chart_data["start_date"],
            y=chart_data["interventions_per_1000km"],
            mode="lines",
            name="Daily",
            line=dict(color=CHART_GREY_COLOR, width=1.5),
        )
    )

    # Thick blue line: the 7-day average (shows the real trend)
    trend_figure.add_trace(
        go.Scatter(
            x=chart_data["start_date"],
            y=chart_data["interventions_per_1000km_7d"],
            mode="lines",
            name="7-day average",
            line=dict(color=CHART_MAIN_COLOR, width=3.5),
        )
    )

    # Dashed vertical lines for the software releases. We only draw a line
    # when its date is inside the dates being shown.
    first_shown_date = chart_data["start_date"].min()
    last_shown_date = chart_data["start_date"].max()
    for marker_date, marker_label in [
        (V22_START_DATE, "v2.2 starts"),
        (V23_START_DATE, "v2.3 starts on half the fleet"),
    ]:
        if first_shown_date <= pd.to_datetime(marker_date) <= last_shown_date:
            # yref="paper": 0 is the bottom of the chart and 1 is the top
            trend_figure.add_shape(
                type="line",
                x0=marker_date,
                x1=marker_date,
                y0=0,
                y1=1,
                yref="paper",
                line=dict(color=CHART_HIGHLIGHT_COLOR, width=1.5, dash="dash"),
            )
            trend_figure.add_annotation(
                x=marker_date,
                y=1,
                yref="paper",
                text=marker_label,
                showarrow=False,
                xanchor="left",
                yanchor="bottom",
                font=dict(color=CHART_HIGHLIGHT_COLOR, size=12),
            )

    trend_figure.update_layout(
        title="Interventions per 1,000 km: daily and 7-day average",
        xaxis_title="Date",
        yaxis_title="Interventions per 1,000 km",
        template=CHART_TEMPLATE,
        height=CHART_HEIGHT,
        legend=dict(orientation="h", yanchor="top", y=-0.2, x=0),
        margin=dict(t=80),
    )
    # Start at zero so the size of changes is not exaggerated
    trend_figure.update_yaxes(rangemode="tozero")
    return trend_figure


def make_version_bar_chart(version_dataframe):
    """One bar per software version; the worst version is orange."""
    chart_data = version_dataframe.sort_values("software_version")
    highest_value = chart_data["interventions_per_1000km"].max()

    # One colour per bar: orange for the worst, blue for the others
    bar_colors = []
    for bar_value in chart_data["interventions_per_1000km"]:
        if bar_value == highest_value:
            bar_colors.append(CHART_HIGHLIGHT_COLOR)
        else:
            bar_colors.append(CHART_MAIN_COLOR)

    version_figure = go.Figure(
        go.Bar(
            x=chart_data["software_version"],
            y=chart_data["interventions_per_1000km"],
            marker_color=bar_colors,
            text=chart_data["interventions_per_1000km"],
            texttemplate="%{text:.1f}",
            textposition="outside",
        )
    )
    version_figure.update_layout(
        title="Interventions per 1,000 km by software version",
        xaxis_title="Software version",
        yaxis_title="Interventions per 1,000 km",
        template=CHART_TEMPLATE,
        height=CHART_HEIGHT,
    )
    # Leave 15% room above the tallest bar for its label
    version_figure.update_yaxes(range=[0, highest_value * 1.15])
    return version_figure


def make_pareto_chart(reasons_dataframe):
    """Bars of interventions per reason plus a cumulative % line."""
    # Add up to ONE row per reason, biggest first
    reason_totals = (
        reasons_dataframe.groupby("reason", as_index=False)["interventions"]
        .sum()
        .sort_values("interventions", ascending=False)
    )

    # Running total divided by the grand total = cumulative share
    grand_total = reason_totals["interventions"].sum()
    reason_totals["cumulative_percent"] = (
        reason_totals["interventions"].cumsum() / grand_total * 100
    )

    # A chart with two y axes: counts on the left, percent on the right
    pareto_figure = make_subplots(specs=[[{"secondary_y": True}]])
    pareto_figure.add_trace(
        go.Bar(
            x=reason_totals["reason"],
            y=reason_totals["interventions"],
            name="Interventions",
            marker_color=CHART_MAIN_COLOR,
        ),
        secondary_y=False,
    )
    pareto_figure.add_trace(
        go.Scatter(
            x=reason_totals["reason"],
            y=reason_totals["cumulative_percent"],
            name="Cumulative share",
            mode="lines+markers",
            line=dict(color=CHART_HIGHLIGHT_COLOR, width=3),
        ),
        secondary_y=True,
    )
    pareto_figure.update_layout(
        title="Why robots need help (Pareto)",
        template=CHART_TEMPLATE,
        height=CHART_HEIGHT,
        legend=dict(orientation="h", yanchor="top", y=-0.25, x=0),
    )
    pareto_figure.update_xaxes(title_text="Reason for intervention")
    pareto_figure.update_yaxes(title_text="Number of interventions", secondary_y=False)
    pareto_figure.update_yaxes(
        title_text="Cumulative share of interventions",
        range=[0, 100],
        ticksuffix="%",
        secondary_y=True,
    )
    return pareto_figure


def make_zone_bar_chart(zone_dataframe):
    """One horizontal bar per zone; the worst zone is orange and on top."""
    chart_data = zone_dataframe.sort_values("interventions_per_1000km", ascending=False)
    highest_value = chart_data["interventions_per_1000km"].max()

    bar_colors = []
    for bar_value in chart_data["interventions_per_1000km"]:
        if bar_value == highest_value:
            bar_colors.append(CHART_HIGHLIGHT_COLOR)
        else:
            bar_colors.append(CHART_MAIN_COLOR)

    zone_figure = go.Figure(
        go.Bar(
            x=chart_data["interventions_per_1000km"],
            y=chart_data["zone"],
            orientation="h",
            marker_color=bar_colors,
            text=chart_data["interventions_per_1000km"],
            texttemplate="%{text:.1f}",
            textposition="outside",
        )
    )
    zone_figure.update_layout(
        title="Interventions per 1,000 km by zone",
        xaxis_title="Interventions per 1,000 km",
        yaxis_title="Zone",
        template=CHART_TEMPLATE,
        height=CHART_HEIGHT,
    )
    # Plotly draws the first row at the bottom, so reverse the y axis
    zone_figure.update_yaxes(autorange="reversed")
    zone_figure.update_xaxes(range=[0, highest_value * 1.15])
    return zone_figure


def make_on_time_heatmap(hour_zone_dataframe):
    """Heatmap: zones as rows, start hours as columns, on-time % as colour."""
    # Add the counts first and divide afterwards (ratio of sums)
    cell_totals = hour_zone_dataframe.groupby(["zone", "start_hour"], as_index=False)[
        ["on_time_deliveries", "completed_deliveries"]
    ].sum()

    # On-time % per cell; empty where nothing was completed
    safe_completed = cell_totals["completed_deliveries"].where(
        cell_totals["completed_deliveries"] > 0
    )
    cell_totals["on_time_percent"] = (
        cell_totals["on_time_deliveries"] / safe_completed * 100
    )

    # One row per zone, one column per hour
    heatmap_grid = cell_totals.pivot(
        index="zone", columns="start_hour", values="on_time_percent"
    )

    # Put the zone with the lowest overall on-time % at the top
    zone_totals = cell_totals.groupby("zone")[
        ["on_time_deliveries", "completed_deliveries"]
    ].sum()
    zone_on_time = zone_totals["on_time_deliveries"] / zone_totals[
        "completed_deliveries"
    ].where(zone_totals["completed_deliveries"] > 0)
    zone_order = zone_on_time.sort_values().index.tolist()
    heatmap_grid = heatmap_grid.loc[zone_order]

    # RdYlBu runs red (low) to blue (high), so late slots look warm
    heatmap_figure = go.Figure(
        go.Heatmap(
            z=heatmap_grid.values,
            x=[f"{int(hour)}:00" for hour in heatmap_grid.columns],
            y=heatmap_grid.index.tolist(),
            colorscale="RdYlBu",
            colorbar=dict(title="On-time %"),
            hovertemplate=(
                "Zone: %{y}<br>Start hour: %{x}<br>On-time: %{z:.1f}%<extra></extra>"
            ),
        )
    )
    heatmap_figure.update_layout(
        title="On-time % by zone and hour",
        xaxis_title="Trip start hour",
        yaxis_title="Zone",
        template=CHART_TEMPLATE,
        height=CHART_HEIGHT,
    )
    return heatmap_figure


def make_cost_by_daytype_chart(daytype_dataframe):
    """Bars for cost per delivery on weekdays and weekends."""
    # Show weekday first, then weekend. If a filter leaves only one of them,
    # draw just that one instead of crashing.
    available_day_types = set(daytype_dataframe["day_type"])
    day_type_order = [
        day_type
        for day_type in ["weekday", "weekend"]
        if day_type in available_day_types
    ]
    chart_data = (
        daytype_dataframe.set_index("day_type").loc[day_type_order].reset_index()
    )

    # The dearer bar is orange, the cheaper one is blue
    highest_cost = chart_data["cost_per_delivery"].max()
    bar_colors = []
    for bar_value in chart_data["cost_per_delivery"]:
        if bar_value == highest_cost:
            bar_colors.append(CHART_HIGHLIGHT_COLOR)
        else:
            bar_colors.append(CHART_MAIN_COLOR)

    cost_figure = go.Figure(
        go.Bar(
            x=chart_data["day_type"],
            y=chart_data["cost_per_delivery"],
            marker_color=bar_colors,
            text=chart_data["cost_per_delivery"],
            texttemplate="$%{text:.2f}",
            textposition="outside",
        )
    )
    cost_figure.update_layout(
        title="Cost per delivery: weekday vs weekend",
        xaxis_title="Type of day",
        yaxis_title="Cost per delivery ($)",
        template=CHART_TEMPLATE,
        height=CHART_HEIGHT,
    )
    cost_figure.update_yaxes(range=[0, highest_cost * 1.2])
    return cost_figure


def make_robot_drain_chart(robot_dataframe):
    """One bar per robot for battery used per km; the 5 worst are orange."""
    chart_data = robot_dataframe.sort_values("avg_charge_used_per_km", ascending=False)

    bar_colors = []
    for rank_value in chart_data["drain_rank"]:
        if rank_value <= 5:
            bar_colors.append(CHART_HIGHLIGHT_COLOR)
        else:
            bar_colors.append(CHART_GREY_COLOR)

    drain_figure = go.Figure(
        go.Bar(
            x=chart_data["robot_id"],
            y=chart_data["avg_charge_used_per_km"],
            marker_color=bar_colors,
            hovertemplate="Robot: %{x}<br>Charge used: %{y:.2f}% per km<extra></extra>",
        )
    )
    drain_figure.update_layout(
        title="Battery used per km, by robot",
        xaxis_title="Robot",
        yaxis_title="Charge used (% per km)",
        template=CHART_TEMPLATE,
        height=CHART_HEIGHT,
    )
    # Turn the 50 robot names sideways so they do not overlap
    drain_figure.update_xaxes(tickangle=-60, tickfont=dict(size=9))
    drain_figure.update_yaxes(rangemode="tozero")
    return drain_figure


def prepare_robot_table(robot_dataframe):
    """A readable table sorted by drain rank, with friendly column names."""
    sorted_table = robot_dataframe.sort_values("drain_rank")
    return pd.DataFrame(
        {
            "Robot": sorted_table["robot_id"],
            "Battery health": sorted_table["battery_health"].round(2),
            "Missions": sorted_table["missions"].astype(int),
            "km": sorted_table["km"].round(1),
            "Interventions per 1,000 km": sorted_table[
                "interventions_per_1000km"
            ].round(1),
            "Charge used per km (%)": sorted_table["avg_charge_used_per_km"].round(2),
            "Battery failures": sorted_table["battery_failures"].astype(int),
            # The failure rate is stored as a share, so show it as a percent
            "Failure rate (%)": (sorted_table["battery_failure_rate"] * 100).round(2),
        }
    )


# =====================================================================
# PAGE: TITLE
# =====================================================================
st.title("Autonomous Delivery Fleet Analytics")
st.write("Synthetic data for a simulated sidewalk-robot fleet.")

# =====================================================================
# SIDEBAR: FILTERS
# =====================================================================
st.sidebar.header("Filters")

# Find the full date range and every filter value that exists in the data
date_bounds_table = run_query(
    "SELECT MIN(start_date) AS first_date, MAX(start_date) AS last_date "
    "FROM v_daily_slice"
)
data_first_date = pd.to_datetime(date_bounds_table["first_date"].iloc[0]).date()
data_last_date = pd.to_datetime(date_bounds_table["last_date"].iloc[0]).date()

all_zones = run_query("SELECT DISTINCT zone FROM v_daily_slice ORDER BY zone")[
    "zone"
].tolist()
all_versions = run_query(
    "SELECT DISTINCT software_version FROM v_daily_slice ORDER BY software_version"
)["software_version"].tolist()
all_weathers = run_query(
    "SELECT DISTINCT weather FROM v_daily_slice ORDER BY weather"
)["weather"].tolist()

# The date range picker returns two dates once the visitor has picked both
selected_dates = st.sidebar.date_input(
    "Date range",
    value=(data_first_date, data_last_date),
    min_value=data_first_date,
    max_value=data_last_date,
)

# Everything is selected by default in the three multiselect boxes
selected_zones = st.sidebar.multiselect("Zones", options=all_zones, default=all_zones)
selected_versions = st.sidebar.multiselect(
    "Software versions", options=all_versions, default=all_versions
)
selected_weathers = st.sidebar.multiselect(
    "Weather", options=all_weathers, default=all_weathers
)

# A short note about the data, tucked away in the sidebar
with st.sidebar.expander("About this data"):
    st.write(
        "All data is synthetic: a simulated fleet of 50 sidewalk robots over 90 "
        "days. The patterns you see (a software regression, rain, a hard zone, "
        "tired batteries) were planted on purpose for this demo. "
        "No real company or customer data is used."
    )

# Stop with a friendly message if the visitor's choices leave nothing to show
if not isinstance(selected_dates, tuple) or len(selected_dates) != 2:
    st.info("Pick both a start date and an end date in the sidebar.")
    st.stop()
if not selected_zones or not selected_versions or not selected_weathers:
    st.warning("Select at least one zone, one software version and one weather type.")
    st.stop()

selected_first_date, selected_last_date = selected_dates

# Load the rows that match every filter
filtered_slice = load_slice(
    selected_first_date,
    selected_last_date,
    selected_zones,
    selected_versions,
    selected_weathers,
)
if filtered_slice.empty:
    st.warning("No missions match these filters. Try widening them.")
    st.stop()

# Dates as real datetime values, so we can group and roll over days
filtered_slice["start_date"] = pd.to_datetime(filtered_slice["start_date"])

# =====================================================================
# KPI TILES (with change versus the previous period of the same length)
# =====================================================================
current_kpis = calculate_kpis(filtered_slice)

# The previous period ends the day before the selected period starts and
# has the same number of days. If it would begin before the data starts,
# we skip the comparison.
selected_day_count = (selected_last_date - selected_first_date).days + 1
previous_last_date = selected_first_date - pd.Timedelta(days=1)
previous_first_date = previous_last_date - pd.Timedelta(days=selected_day_count - 1)

previous_kpis = None
if previous_first_date >= data_first_date:
    previous_slice = load_slice(
        previous_first_date,
        previous_last_date,
        selected_zones,
        selected_versions,
        selected_weathers,
    )
    if not previous_slice.empty:
        previous_kpis = calculate_kpis(previous_slice)


def previous_value_of(kpi_name):
    """The previous-period value of one KPI, or None if there is none."""
    if previous_kpis is None:
        return None
    return previous_kpis[kpi_name]


tile_autonomy, tile_interventions, tile_on_time, tile_cost = st.columns(4)

# Higher autonomy and on-time % are good, so up is green (default colours)
tile_autonomy.metric(
    "Autonomy rate",
    format_percent(current_kpis["autonomy_rate"]),
    build_delta_text(
        current_kpis["autonomy_rate"], previous_value_of("autonomy_rate"), "percent"
    ),
)
# Lower is better for interventions and cost, so "inverse" flips the colours
tile_interventions.metric(
    "Interventions per 1,000 km",
    format_number(current_kpis["interventions_per_1000km"]),
    build_delta_text(
        current_kpis["interventions_per_1000km"],
        previous_value_of("interventions_per_1000km"),
        "number",
    ),
    delta_color="inverse",
)
tile_on_time.metric(
    "On-time %",
    format_percent(current_kpis["on_time_pct"]),
    build_delta_text(
        current_kpis["on_time_pct"], previous_value_of("on_time_pct"), "percent"
    ),
)
tile_cost.metric(
    "Cost per delivery",
    format_dollars(current_kpis["cost_per_delivery"]),
    build_delta_text(
        current_kpis["cost_per_delivery"],
        previous_value_of("cost_per_delivery"),
        "dollars",
    ),
    delta_color="inverse",
)

if previous_kpis is None:
    st.caption("No earlier period of the same length exists, so no change is shown.")
else:
    st.caption(
        f"Arrows compare with the previous {selected_day_count} days "
        f"({previous_first_date} to {previous_last_date})."
    )

# =====================================================================
# TABS
# =====================================================================
overview_tab, reliability_tab, delivery_tab, robots_tab = st.tabs(
    ["Overview", "Reliability", "Delivery and cost", "Robots"]
)

# ---------------------------------------------------------------------
# TAB 1: OVERVIEW
# ---------------------------------------------------------------------
with overview_tab:
    # Add up interventions and km for each day
    daily_totals = (
        filtered_slice.groupby("start_date")[["interventions", "km"]].sum().sort_index()
    )

    # 7-day rolling totals: add 7 days of interventions and 7 days of km first,
    # THEN divide. Never average the daily ratios.
    rolling_totals = daily_totals.rolling("7D").sum()

    safe_daily_km = daily_totals["km"].where(daily_totals["km"] > 0)
    safe_rolling_km = rolling_totals["km"].where(rolling_totals["km"] > 0)

    # A table with the column names that make_daily_trend_chart expects
    daily_table = pd.DataFrame(
        {
            "start_date": daily_totals.index,
            "interventions_per_1000km": (
                daily_totals["interventions"] / safe_daily_km * 1000
            ).values,
            "interventions_per_1000km_7d": (
                rolling_totals["interventions"] / safe_rolling_km * 1000
            ).values,
        }
    )

    st.plotly_chart(make_daily_trend_chart(daily_table))
    st.caption(
        "The thick line is the 7-day average. Look for the dip after v2.2 and "
        "the rise after v2.3 reached half the fleet."
    )

# ---------------------------------------------------------------------
# TAB 2: RELIABILITY
# ---------------------------------------------------------------------
with reliability_tab:
    # (a) By software version, from the filtered slice
    version_summary = summarise_slice(filtered_slice, "software_version").dropna(
        subset=["interventions_per_1000km"]
    )
    if not version_summary.empty:
        st.plotly_chart(make_version_bar_chart(version_summary))
        st.caption("The orange bar is the version that needs human help most often.")

    # (b) By zone, from the filtered slice
    zone_summary = summarise_slice(filtered_slice, "zone").dropna(
        subset=["interventions_per_1000km"]
    )
    if not zone_summary.empty:
        st.plotly_chart(make_zone_bar_chart(zone_summary))
        st.caption("The orange bar is the zone where robots struggle most.")

    # (c) Reasons: zone, version and weather filters apply, the date range does not
    reasons_table = run_query(
        """
        SELECT reason, interventions
        FROM v_intervention_reasons
        WHERE list_contains(CAST(? AS VARCHAR[]), zone)
          AND list_contains(CAST(? AS VARCHAR[]), software_version)
          AND list_contains(CAST(? AS VARCHAR[]), weather)
        """,
        [list(selected_zones), list(selected_versions), list(selected_weathers)],
    )
    if reasons_table.empty:
        st.info("No interventions match the zone, version and weather filters.")
    else:
        st.plotly_chart(make_pareto_chart(reasons_table))
        st.caption(
            "A few reasons cause most interventions. The date range does not "
            "apply to this chart; zone, version and weather do."
        )

# ---------------------------------------------------------------------
# TAB 3: DELIVERY AND COST
# ---------------------------------------------------------------------
with delivery_tab:
    # (a) Heatmap: only the zone filter applies to this view
    hour_zone_table = run_query(
        """
        SELECT start_hour, zone, on_time_deliveries, completed_deliveries
        FROM v_by_hour
        WHERE list_contains(CAST(? AS VARCHAR[]), zone)
        """,
        [list(selected_zones)],
    )
    if hour_zone_table["completed_deliveries"].sum() > 0:
        st.plotly_chart(make_on_time_heatmap(hour_zone_table))
        st.caption(
            "Zone filter only; other filters do not apply. Warm colours mark "
            "slots where deliveries are late."
        )

    # (b) Cost per delivery on weekdays versus weekends, from the filtered slice
    daytype_slice = filtered_slice.copy()
    daytype_slice["day_type"] = daytype_slice["is_weekend"].astype(int).map(
        {0: "weekday", 1: "weekend"}
    )
    daytype_summary = summarise_slice(daytype_slice, "day_type").dropna(
        subset=["cost_per_delivery"]
    )
    if not daytype_summary.empty:
        st.plotly_chart(make_cost_by_daytype_chart(daytype_summary))
        st.caption(
            "The fixed daily cost of each robot is shared across more trips on "
            "busy days, so cost per delivery drops."
        )

# ---------------------------------------------------------------------
# TAB 4: ROBOTS
# ---------------------------------------------------------------------
with robots_tab:
    robot_view_table = run_query("SELECT * FROM v_by_robot")

    st.plotly_chart(make_robot_drain_chart(robot_view_table))
    st.caption(
        "Whole period; filters do not apply. The five orange bars are robots "
        "whose batteries drain fastest."
    )

    st.dataframe(prepare_robot_table(robot_view_table), hide_index=True)
    st.caption("Whole period; filters do not apply.")

# =====================================================================
# DEFINITIONS AND FOOTER
# =====================================================================
with st.expander("How the KPIs are defined"):
    st.markdown(
        """
| KPI | Plain-English definition |
| --- | --- |
| Autonomy rate | Missions with zero human interventions, divided by all missions |
| Interventions per 1,000 km | Number of interventions, divided by km driven, times 1,000 |
| On-time % | Completed deliveries that arrived by the promised time, divided by completed deliveries |
| Cost per delivery | Total cost of all missions (failed ones too), divided by completed deliveries |

Every KPI is a ratio of sums: one total divided by another total. We never
average daily percentages, because that gives wrong answers.
"""
    )

st.divider()
st.caption(f"Built by {AUTHOR_NAME} with Python, DuckDB, SQL and Streamlit.")
