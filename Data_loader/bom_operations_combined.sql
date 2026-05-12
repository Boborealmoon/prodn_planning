-- BOM Operations — Combined (PostgreSQL + Google Sheet)
-- ⚠  Run against planner.db (SQLite), NOT PostgreSQL.
--    Requires both staging tables to be loaded first:
--      1. erp_sync.py --query-file Data_loader/bom_operations.sql --table bom_operations_staging
--      2. python_32bit/fetch_setup_time.py
--
-- Joins bom_operations_staging (from PostgreSQL) with setup_time_staging (from Google Sheet)
-- on inventory_code = part_no AND op_no (integer match).
-- Adds setup_time and cycle_time from the Google Sheet to each BOM operation row.
--
-- Final columns:
--   inventory_code, bom_code, stage_no, stage_desc, op_no,
--   machine_no, setup_time, cycle_time

SELECT
    b.inventory_code,
    b.bom_code,
    b.stage_no,
    b.stage_desc,
    CAST(b.op_no AS INTEGER)          AS op_no,
    b.machine_no,
    CAST(s.setup_time AS INTEGER)     AS setup_time,
    CAST(s.cycle_time AS REAL)        AS cycle_time
FROM bom_operations_staging b
LEFT JOIN setup_time_staging s
    ON  s.part_no             = b.inventory_code
    AND CAST(s.op_no AS INTEGER) = CAST(b.op_no AS INTEGER)
ORDER BY
    b.inventory_code,
    b.bom_code,
    CAST(b.op_no AS INTEGER);
