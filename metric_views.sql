-- =====================================================================
-- METRIC VIEWS LAYER - COMBINED DEPLOYMENT SCRIPT
-- Consolidated view definitions for fleet operations metrics layer
-- =====================================================================

-- -----------------------------------------------------
-- View Segment: missions_clean
-- -----------------------------------------------------
CREATE OR REPLACE VIEW missions_clean AS
-- missions_clean
SELECT
    mission_id,
    robot_id,
    zone,
    start_time,
    distance_km,
    promised_eta,
    actual_arrival,
    status,
    failure_reason,
    software_version,
    weather
FROM missions
-- Rule 2: Remove primary-key duplicates by preserving exactly one row per mission_id
-- (Sorting by start_time to keep the first chronological instance)
WINDOW w AS (PARTITION BY mission_id ORDER BY start_time)
QUALIFY ROW_NUMBER() OVER w = 1
-- Rule 3: Remove records with corrupted non-positive travel distances
AND distance_km > 0.0
-- Rule 4: Remove trips where physical arrival incorrectly precedes departure (only affecting completed status)
AND (actual_arrival IS NULL OR actual_arrival >= start_time);

-- -----------------------------------------------------
-- View Segment: v_mission_facts
-- -----------------------------------------------------
CREATE OR REPLACE VIEW v_mission_facts AS
-- v_mission_facts
WITH intervention_totals AS (
    -- Roll up interventions to a single row per mission to avoid the join trap
    SELECT 
        mission_id,
        COUNT(*) AS raw_intervention_count,
        SUM(duration_sec) AS raw_intervention_seconds
    FROM interventions
    GROUP BY mission_id
)
SELECT
    m.mission_id,
    m.robot_id,
    m.zone,
    m.start_time,
    CAST(m.start_time AS DATE) AS start_date,
    EXTRACT(HOUR FROM m.start_time) AS start_hour,
    -- Check if day of week is Saturday (6) or Sunday (7) using DuckDB ISODOW
    CASE WHEN EXTRACT(ISODOW FROM m.start_time) IN (6, 7) THEN 1 ELSE 0 END AS is_weekend,
    m.distance_km,
    m.promised_eta,
    m.actual_arrival,
    m.status,
    m.failure_reason,
    m.software_version,
    m.weather,
    
    -- Safely coalesce rolled up interventions. LEFT JOIN ensures trips with zero interventions are preserved.
    COALESCE(i.raw_intervention_count, 0) AS intervention_count,
    COALESCE(i.raw_intervention_seconds, 0) AS intervention_seconds,
    
    -- Battery logs details
    b.charge_start_pct,
    b.charge_end_pct,
    (b.charge_start_pct - b.charge_end_pct) AS charge_used_pct,
    
    -- Status indicators
    CASE WHEN m.status = 'completed' THEN 1 ELSE 0 END AS is_completed,
    CASE WHEN m.status = 'completed' AND m.actual_arrival <= m.promised_eta THEN 1 ELSE 0 END AS is_on_time,
    CASE WHEN COALESCE(i.raw_intervention_count, 0) = 0 THEN 1 ELSE 0 END AS has_zero_interventions,
    
    -- Cost Columns utilizing scalar subqueries for configuration lookup
    (m.distance_km * (
        (SELECT CAST(value AS DOUBLE) FROM cost_assumptions WHERE item = 'maintenance_per_km') +
        (SELECT CAST(value AS DOUBLE) FROM cost_assumptions WHERE item = 'energy_per_km')
    )) AS km_cost,
    
    ((COALESCE(i.raw_intervention_seconds, 0) / 60.0 * 
        (SELECT CAST(value AS DOUBLE) FROM cost_assumptions WHERE item = 'operator_per_minute')) +
     (COALESCE(i.raw_intervention_count, 0) * 
        (SELECT CAST(value AS DOUBLE) FROM cost_assumptions WHERE item = 'handoff_per_intervention')
    )) AS intervention_cost,
    
    (SELECT CAST(value AS DOUBLE) FROM cost_assumptions WHERE item = 'overhead_per_mission') AS overhead_cost,
    
    -- Total variable cost sum (excluding daily asset depreciation)
    ((m.distance_km * (
        (SELECT CAST(value AS DOUBLE) FROM cost_assumptions WHERE item = 'maintenance_per_km') +
        (SELECT CAST(value AS DOUBLE) FROM cost_assumptions WHERE item = 'energy_per_km')
    )) + 
     (COALESCE(i.raw_intervention_seconds, 0) / 60.0 * 
        (SELECT CAST(value AS DOUBLE) FROM cost_assumptions WHERE item = 'operator_per_minute')) +
     (COALESCE(i.raw_intervention_count, 0) * 
        (SELECT CAST(value AS DOUBLE) FROM cost_assumptions WHERE item = 'handoff_per_intervention')) +
     (SELECT CAST(value AS DOUBLE) FROM cost_assumptions WHERE item = 'overhead_per_mission')
    ) AS variable_cost

FROM missions_clean m
LEFT JOIN intervention_totals i ON m.mission_id = i.mission_id
JOIN battery_logs b ON m.mission_id = b.mission_id;

-- -----------------------------------------------------
-- View Segment: v_daily_slice
-- -----------------------------------------------------
CREATE OR REPLACE VIEW v_daily_slice AS
-- v_daily_slice
-- v_daily_slice
-- This view aggregates mission facts into daily, zonal, software, and weather segments.
-- It contains ONLY additive sum metrics to allow mathematically sound downstream aggregations (ratio of sums, not average of ratios).

WITH aggregated_slices AS (
    -- Step 1: Group facts view by keys and sum up variables
    SELECT
        start_date,
        zone,
        software_version,
        weather,
        ANY_VALUE(is_weekend) AS is_weekend, -- Group is aligned since all missions on a day share is_weekend status
        COUNT(*) AS missions,
        SUM(is_completed) AS completed_deliveries,
        SUM(has_zero_interventions) AS zero_intervention_missions,
        SUM(intervention_count) AS interventions,
        SUM(intervention_seconds) AS intervention_seconds,
        SUM(distance_km) AS km,
        SUM(is_on_time) AS on_time_deliveries,
        SUM(variable_cost) AS variable_cost
    FROM v_mission_facts
    GROUP BY start_date, zone, software_version, weather
),
depreciation_allocation AS (
    -- Step 2: Sum up total daily missions to share daily depreciation cost proportionally
    SELECT
        *,
        SUM(missions) OVER(PARTITION BY start_date) AS total_daily_missions
    FROM aggregated_slices
)
SELECT
    start_date,
    zone,
    software_version,
    weather,
    is_weekend,
    missions,
    completed_deliveries,
    zero_intervention_missions,
    interventions,
    intervention_seconds,
    km,
    on_time_deliveries,
    variable_cost,
    
    -- Step 3: Compute depreciation cost proportionally.
    -- Daily fleet cost = Total robots * Depreciation rate per robot per day
    -- Slice cost = Daily fleet cost * (Slice missions / Daily total missions)
    ((
        (SELECT COUNT(*) FROM robots) * 
        (SELECT CAST(value AS DOUBLE) FROM cost_assumptions WHERE item = 'robot_depreciation_per_day')
    ) * (CAST(missions AS DOUBLE) / total_daily_missions)) AS depreciation_cost,
    
    -- Step 4: Calculate total cost
    (variable_cost + (
        (SELECT COUNT(*) FROM robots) * 
        (SELECT CAST(value AS DOUBLE) FROM cost_assumptions WHERE item = 'robot_depreciation_per_day')
    ) * (CAST(missions AS DOUBLE) / total_daily_missions)) AS total_cost

FROM depreciation_allocation;

-- -----------------------------------------------------
-- View Segment: v_kpi_daily
-- -----------------------------------------------------
CREATE OR REPLACE VIEW v_kpi_daily AS
-- v_kpi_daily
-- v_kpi_daily
-- This view rolls up daily slice records into exactly one row per calendar day.
-- It implements safe divisions and rolling 7-day averages of operational metrics.

WITH daily_base AS (
    -- Step 1: Sum up all slices per start_date to establish daily absolute totals
    SELECT
        start_date,
        ANY_VALUE(is_weekend) AS is_weekend,
        SUM(missions) AS missions,
        SUM(completed_deliveries) AS completed_deliveries,
        SUM(zero_intervention_missions) AS zero_intervention_missions,
        SUM(interventions) AS interventions,
        SUM(intervention_seconds) AS intervention_seconds,
        SUM(km) AS km,
        SUM(on_time_deliveries) AS on_time_deliveries,
        SUM(total_cost) AS total_cost
    FROM v_daily_slice
    GROUP BY start_date
),
rolling_sums AS (
    -- Step 2: Calculate window-based numerator and denominator sums over 7 days
    SELECT
        *,
        -- Window frames defined to look back 6 preceding rows + current row (chronological order)
        SUM(interventions) OVER(ORDER BY start_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS rolling_interventions_7d,
        SUM(km) OVER(ORDER BY start_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS rolling_km_7d,
        SUM(on_time_deliveries) OVER(ORDER BY start_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS rolling_on_time_7d,
        SUM(completed_deliveries) OVER(ORDER BY start_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS rolling_completed_7d,
        SUM(total_cost) OVER(ORDER BY start_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS rolling_cost_7d
    FROM daily_base
)
SELECT
    start_date,
    is_weekend,
    missions,
    completed_deliveries,
    zero_intervention_missions,
    interventions,
    km,
    on_time_deliveries,
    total_cost,
    
    -- Step 3: Compute daily single-day metrics using NULLIF to prevent division-by-zero crashes
    (CAST(zero_intervention_missions AS DOUBLE) / NULLIF(missions, 0)) AS autonomy_rate,
    (CAST(interventions AS DOUBLE) / NULLIF(km, 0) * 1000.0) AS interventions_per_1000km,
    (CAST(on_time_deliveries AS DOUBLE) / NULLIF(completed_deliveries, 0)) AS on_time_pct,
    (total_cost / NULLIF(completed_deliveries, 0)) AS cost_per_delivery,
    
    -- Step 4: Compute rolling 7-day KPIs from aggregate sums to maintain mathematical weights
    (CAST(rolling_interventions_7d AS DOUBLE) / NULLIF(rolling_km_7d, 0) * 1000.0) AS interventions_per_1000km_7d,
    (CAST(rolling_on_time_7d AS DOUBLE) / NULLIF(rolling_completed_7d, 0)) AS on_time_pct_7d,
    (rolling_cost_7d / NULLIF(rolling_completed_7d, 0)) AS cost_per_delivery_7d

FROM rolling_sums;

-- -----------------------------------------------------
-- View Segment: v_by_version
-- -----------------------------------------------------
CREATE OR REPLACE VIEW v_by_version AS
-- v_by_version
-- v_by_version
-- Segmented operational metrics and KPIs grouped by software firmware version.

SELECT
    software_version,
    SUM(missions) AS missions,
    SUM(completed_deliveries) AS completed_deliveries,
    SUM(zero_intervention_missions) AS zero_intervention_missions,
    SUM(interventions) AS interventions,
    SUM(km) AS km,
    SUM(on_time_deliveries) AS on_time_deliveries,
    SUM(total_cost) AS total_cost,
    
    -- KPIs calculated as ratios of sums
    (CAST(SUM(zero_intervention_missions) AS DOUBLE) / NULLIF(SUM(missions), 0)) AS autonomy_rate,
    (CAST(SUM(interventions) AS DOUBLE) / NULLIF(SUM(km), 0) * 1000.0) AS interventions_per_1000km,
    (CAST(SUM(on_time_deliveries) AS DOUBLE) / NULLIF(SUM(completed_deliveries), 0)) AS on_time_pct,
    (SUM(total_cost) / NULLIF(SUM(completed_deliveries), 0)) AS cost_per_delivery
FROM v_daily_slice
GROUP BY software_version;

-- -----------------------------------------------------
-- View Segment: v_by_zone
-- -----------------------------------------------------
CREATE OR REPLACE VIEW v_by_zone AS
-- v_by_zone
-- v_by_zone
-- Segmented operational metrics and KPIs grouped by geographic zone.

SELECT
    zone,
    SUM(missions) AS missions,
    SUM(completed_deliveries) AS completed_deliveries,
    SUM(zero_intervention_missions) AS zero_intervention_missions,
    SUM(interventions) AS interventions,
    SUM(km) AS km,
    SUM(on_time_deliveries) AS on_time_deliveries,
    SUM(total_cost) AS total_cost,
    
    -- KPIs calculated as ratios of sums
    (CAST(SUM(zero_intervention_missions) AS DOUBLE) / NULLIF(SUM(missions), 0)) AS autonomy_rate,
    (CAST(SUM(interventions) AS DOUBLE) / NULLIF(SUM(km), 0) * 1000.0) AS interventions_per_1000km,
    (CAST(SUM(on_time_deliveries) AS DOUBLE) / NULLIF(SUM(completed_deliveries), 0)) AS on_time_pct,
    (SUM(total_cost) / NULLIF(SUM(completed_deliveries), 0)) AS cost_per_delivery
FROM v_daily_slice
GROUP BY zone;

-- -----------------------------------------------------
-- View Segment: v_by_weather
-- -----------------------------------------------------
CREATE OR REPLACE VIEW v_by_weather AS
-- v_by_weather
-- v_by_weather
-- Segmented operational metrics and KPIs grouped by weather status.

SELECT
    weather,
    SUM(missions) AS missions,
    SUM(completed_deliveries) AS completed_deliveries,
    SUM(zero_intervention_missions) AS zero_intervention_missions,
    SUM(interventions) AS interventions,
    SUM(km) AS km,
    SUM(on_time_deliveries) AS on_time_deliveries,
    SUM(total_cost) AS total_cost,
    
    -- KPIs calculated as ratios of sums
    (CAST(SUM(zero_intervention_missions) AS DOUBLE) / NULLIF(SUM(missions), 0)) AS autonomy_rate,
    (CAST(SUM(interventions) AS DOUBLE) / NULLIF(SUM(km), 0) * 1000.0) AS interventions_per_1000km,
    (CAST(SUM(on_time_deliveries) AS DOUBLE) / NULLIF(SUM(completed_deliveries), 0)) AS on_time_pct,
    (SUM(total_cost) / NULLIF(SUM(completed_deliveries), 0)) AS cost_per_delivery
FROM v_daily_slice
GROUP BY weather;

-- -----------------------------------------------------
-- View Segment: v_by_daytype
-- -----------------------------------------------------
CREATE OR REPLACE VIEW v_by_daytype AS
-- v_by_daytype
-- v_by_daytype
-- Segmented operational metrics and KPIs grouped by day type (weekend vs weekday).

SELECT
    is_weekend,
    CASE WHEN is_weekend = 1 THEN 'weekend' ELSE 'weekday' END AS day_type,
    SUM(missions) AS missions,
    SUM(completed_deliveries) AS completed_deliveries,
    SUM(zero_intervention_missions) AS zero_intervention_missions,
    SUM(interventions) AS interventions,
    SUM(km) AS km,
    SUM(on_time_deliveries) AS on_time_deliveries,
    SUM(total_cost) AS total_cost,
    
    -- KPIs calculated as ratios of sums
    (CAST(SUM(zero_intervention_missions) AS DOUBLE) / NULLIF(SUM(missions), 0)) AS autonomy_rate,
    (CAST(SUM(interventions) AS DOUBLE) / NULLIF(SUM(km), 0) * 1000.0) AS interventions_per_1000km,
    (CAST(SUM(on_time_deliveries) AS DOUBLE) / NULLIF(SUM(completed_deliveries), 0)) AS on_time_pct,
    (SUM(total_cost) / NULLIF(SUM(completed_deliveries), 0)) AS cost_per_delivery
FROM v_daily_slice
GROUP BY is_weekend;

-- -----------------------------------------------------
-- View Segment: v_by_hour
-- -----------------------------------------------------
CREATE OR REPLACE VIEW v_by_hour AS
-- v_by_hour
-- v_by_hour
-- Segmented hourly/spatial operational profile grouped by start_hour and zone.
-- Cost columns are excluded as daily depreciation is not calculated hourly.

SELECT
    start_hour,
    zone,
    COUNT(*)                     AS missions,                    -- ALL missions, failed ones too
    SUM(is_completed)            AS completed_deliveries,
    SUM(is_on_time)              AS on_time_deliveries,
    SUM(intervention_count)      AS interventions,
    SUM(distance_km)             AS km,
    SUM(has_zero_interventions)  AS zero_intervention_missions
    -- keep your three ratio columns below, unchanged:
    -- on_time_pct divides by completed_deliveries; autonomy_rate divides by missions
FROM v_mission_facts
GROUP BY start_hour, zone;

-- -----------------------------------------------------
-- View Segment: v_by_robot
-- -----------------------------------------------------
CREATE OR REPLACE VIEW v_by_robot AS
-- v_by_robot
-- v_by_robot
-- Detailed operational performance and battery consumption rate profiling per physical robot asset.

SELECT
    f.robot_id,
    r.battery_health,
    COUNT(f.mission_id) AS missions,
    SUM(f.distance_km) AS km,
    SUM(f.intervention_count) AS interventions,
    
    -- Safe ratio of sums KPI calculations
    (CAST(SUM(f.intervention_count) AS DOUBLE) / NULLIF(SUM(f.distance_km), 0) * 1000.0) AS interventions_per_1000km,
    (SUM(f.charge_used_pct) / NULLIF(SUM(f.distance_km), 0)) AS avg_charge_used_per_km,
    
    SUM(CASE WHEN f.failure_reason = 'battery_low' THEN 1 ELSE 0 END) AS battery_failures,
    (CAST(SUM(CASE WHEN f.failure_reason = 'battery_low' THEN 1 ELSE 0 END) AS DOUBLE) / NULLIF(COUNT(f.mission_id), 0)) AS battery_failure_rate,
    
    RANK() OVER (ORDER BY (SUM(f.charge_used_pct) / NULLIF(SUM(f.distance_km), 0)) DESC) AS drain_rank
FROM v_mission_facts f
JOIN robots r ON f.robot_id = r.robot_id
GROUP BY f.robot_id, r.battery_health;

-- -----------------------------------------------------
-- View Segment: v_intervention_reasons
-- -----------------------------------------------------
CREATE OR REPLACE VIEW v_intervention_reasons AS
-- v_intervention_reasons
-- v_intervention_reasons
-- Telemetry intervention counts and durations segmented by diagnostic code, firmware, zone, and weather.
-- Joining to missions_clean is crucial because it filters out intervention logs corresponding to corrupted
-- or duplicated trips that were removed during our initial metrics sanitization pass.

SELECT
    i.reason,
    m.software_version,
    m.zone,
    m.weather,
    COUNT(*) AS interventions,
    SUM(i.duration_sec) AS total_seconds
FROM interventions i
INNER JOIN missions_clean m ON i.mission_id = m.mission_id
GROUP BY i.reason, m.software_version, m.zone, m.weather;

