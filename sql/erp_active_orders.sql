-- Fetch active / open orders from the ERP (used to mark PS status as ACTIVE vs COMPLETED).
-- Required output columns:
--   ps_id      TEXT  -- PP / process sheet number (also accepted: source_pp_no, pp_no, pp_partial_no, voucher_no)
--   part_no    TEXT  -- part inventory code (also accepted: inventory_code, item_code, source_inventory_code)
--   bom_code   TEXT  -- BOM reference code
--
-- Replace <active_orders_table> with the actual ERP table name or view.

SELECT
    ps_id,
    part_no,
    bom_code
FROM <active_orders_table>;
