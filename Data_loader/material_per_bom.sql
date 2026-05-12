-- Material per BOM  (also used as "Material per BOM Stage" — identical query)
-- Source: public.inventory_bom_listing
--
-- Keeps only leaf-level (raw) materials: rows where material_inventory_code
-- is never itself a source/finished product (never appears as source_inventory_code).
-- Equivalent to the Power Query filter: not List.Contains([source_inventory_code], [material_inventory_code])
-- followed by Table.Distinct.

SELECT DISTINCT
    bom_code,
    source_inventory_code,
    material_inventory_code,
    description
FROM public.inventory_bom_listing
WHERE material_inventory_code NOT IN (
    SELECT source_inventory_code
    FROM public.inventory_bom_listing
    WHERE source_inventory_code IS NOT NULL
);
