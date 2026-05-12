-- Part Description
-- Source: public.mt_inventory_item_view
--
-- Excludes internal/classification columns that are not needed downstream:
--   pk_no_item, pack_size_code, uom_type, inventory_class_code, inventory_category_code
--
-- Replace SELECT * with an explicit column list once the schema is confirmed,
-- dropping the five columns above.

SELECT *
FROM public.mt_inventory_item_view;
