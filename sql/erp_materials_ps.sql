-- Fetch materials consumed per process sheet from the ERP.
-- Required output columns:
--   pp_voucher               TEXT  -- PP voucher / job number
--   pp_partial_no            TEXT  -- partial/sequence number
--   inventory_code           TEXT  -- finished part inventory code
--   material_inventory_code  TEXT  -- raw material inventory code
--
-- Replace <materials_ps_table> with the actual ERP table name.

SELECT
    pp_voucher,
    pp_partial_no,
    inventory_code,
    material_inventory_code
FROM <materials_ps_table>;
