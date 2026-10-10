# Fleetanalytics

Fleetanalytics is a synthetic fleet operations analytics project for an autonomous delivery robot fleet. It models a 50-robot operation over 90 days, generates mission, battery, and intervention data, cleans and validates the raw records, and exposes a DuckDB-based metrics layer for operational analysis.

The project is designed to answer business questions such as:

- Which robot models or software versions have the highest intervention rate?
- How do weather and weekend demand affect delivery reliability?
- Where are the biggest cost and operational inefficiencies in the fleet?
- Which missions fail due to battery issues, blocked routes, or localization errors?

## Project goals

This repository demonstrates a complete analytics workflow from raw event generation to cleaned fact tables and KPI views:

- Simulate a realistic robot delivery environment
- Generate synthetic operational data for robots, missions, battery usage, and interventions
- Introduce controlled data-quality issues to test cleaning logic
- Validate the cleaned dataset against explicit checks
- Build SQL views for fleet KPIs and aggregate analysis
- Support downstream reporting and dashboarding with DuckDB and Python

## Repository structure

```text
.
├── Fleet_project.ipynb          # Main notebook containing the synthetic data pipeline
├── metric_views.sql             # DuckDB SQL view definitions for KPI and analysis layers
├── requirements.txt            # Python dependencies
├── LICENSE                     # Project license
├── fleet.duckdb                # DuckDB database generated from the pipeline
├── robots.csv                  # Robot master data
├── missions.csv                # Mission-level operational records
├── battery_logs.csv            # Battery charge/start/end logs by mission
├── interventions.csv           # Human intervention events tied to missions
├── cost_assumptions.csv         # Cost input parameters for KPI calculations
├── data_quality_checks.csv      # Validation results for the data-cleaning pipeline
└── README.md                   # Project overview and usage guide
```

## Data model

The project simulates several operational tables:

- robots: robot IDs, battery health, and purchase dates
- days / schedule context: calendar days, weather, weekend flags, and demand multipliers
- missions: delivery missions, location zones, start/end times, distances, promised ETA, performance status, and software version
- battery_logs: charge start/end values and battery consumption by mission
- interventions: operator intervention events, duration, and reason codes

The notebook also intentionally injects dirty data such as:

- duplicate mission records
- negative durations
- zero-distance trips
- invalid timing patterns

These are removed in the `missions_clean` layer to create a trustworthy fact table.

## Key metrics and views

The SQL layer defines a set of analytical views to support fleet analysis:

- `missions_clean`: cleaned mission records with dirty rows removed
- `v_mission_facts`: per-mission fact table with intervention, cost, and battery metrics
- `v_daily_slice`: daily slice-level totals for KPI aggregation
- `v_kpi_daily`: one row per day with operational KPIs
- `v_by_version`: metrics segmented by software version
- `v_by_zone`: metrics segmented by service zone
- `v_by_weather`: metrics segmented by weather conditions
- `v_by_daytype`: metrics for weekday vs. weekend comparisons
- `v_by_robot`: robot-level canary metrics and battery drain ranking
- `v_intervention_reasons`: intervention counts grouped by cause, firmware, zone, and weather

Representative KPIs include:

- autonomy rate
- interventions per 1000 km
- on-time percentage
- cost per delivery
- battery failure rate
- robot-level charge drain per kilometer

## Getting started

### 1. Clone the repository

```bash
git clone https://github.com/avinashnikhil/Fleetanalytics.git
cd Fleetanalytics
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Open the notebook

Open `Fleet_project.ipynb` in Jupyter Notebook or VS Code with Jupyter support to reproduce the synthetic data generation, data cleaning, and validation flow.

### 4. Load the DuckDB metrics layer

If you want to work directly with the SQL metrics layer:

```python
import duckdb

con = duckdb.connect("fleet.duckdb")
con.execute("CREATE OR REPLACE VIEW missions_clean AS SELECT * FROM read_csv_auto('missions.csv');")
# or load the full SQL definitions from metric_views.sql
```

If the database has already been generated, you can query it directly:

```python
import duckdb

con = duckdb.connect("fleet.duckdb")
print(con.execute("SELECT * FROM v_kpi_daily LIMIT 10").fetchdf())
```

## Example SQL queries

Daily KPI trend:

```sql
SELECT start_date, missions, interventions, autonomy_rate, on_time_pct, cost_per_delivery
FROM v_kpi_daily
ORDER BY start_date;
```

Compare versions:

```sql
SELECT software_version, missions, interventions, interventions_per_1000km, on_time_pct, cost_per_delivery
FROM v_by_version
ORDER BY interventions_per_1000km DESC;
```

Compare zones:

```sql
SELECT zone, missions, km, interventions, interventions_per_1000km, autonomy_rate
FROM v_by_zone
ORDER BY interventions_per_1000km DESC;
```

Intervention root causes:

```sql
SELECT reason, SUM(interventions) AS total_interventions, SUM(total_seconds) AS total_seconds
FROM v_intervention_reasons
GROUP BY reason
ORDER BY total_interventions DESC;
```

## Data quality checks

The project includes an explicit data-quality validation file at `data_quality_checks.csv`, which verifies the cleanup logic and shows that the synthetic data pipeline catches planted anomalies as expected.

The checks cover:

- duplicate detection
- negative-duration removal
- zero-distance filtering
- invalid time ordering
- out-of-range validation
- reconciliation across views
- KPI range validation

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.

## Notes

This repository is a synthetic example project intended for analytics and operations simulation education. It is well suited for learning in the following areas:

- Python data pipelines
- pandas and NumPy simulation
- DuckDB SQL analytics
- operational KPI design
- data quality testing and validation
- business intelligence over logistics metrics

If you want to extend the project, good next steps include:

- adding a Streamlit dashboard
- building a more formal star schema
- connecting the SQL views to a BI tool
- generating richer environmental and fleet behavior models

