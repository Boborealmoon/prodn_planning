-- Fetch materials per BOM (bill of materials) from the ERP.
-- Required output columns:
--   bom_code                 TEXT  -- BOM reference code
--   source_inventory_code    TEXT  -- finished part inventory code
--   material_inventory_code  TEXT  -- raw material inventory code
--   description              TEXT  -- material description
--
-- Replace <materials_bom_table> with the actual ERP table name.

SELECT
    bom_code,
    source_inventory_code,
    material_inventory_code,
    description
FROM <materials_bom_table>;
