-- PP Voucher
-- Source: public.mfg_pp_vch
--
-- Navigation/relationship columns (mfg_br_vch, mfg_bv_vch, mfg_mov_vch, mfg_pp_partial,
-- mfg_pp_vch_combined_pp, mfg_split_product_vch, mfg_svv_vch, mt_inventory, mt_inventory_bom,
-- mt_inventory_pack_size, mt_location, pj_est_ost_phs) do not exist as SQL columns — auto-excluded.
--
-- Actual PostgreSQL columns to exclude (replace SELECT * with an explicit column list
-- once the schema is confirmed, dropping these):
--   location_code, bom_desc, pack_size_code, pp_no_of_pack,
--   required_procurement_date, required_start_date, required_end_date,
--   order_release_date, production_due_date, module_code, transaction_type_code,
--   source_shipment_no, segment_1_code, segment_2_code, segment_3_code, segment_4_code,
--   source_revision_no, ost_pp_qty, this_pp_qty, scheduling_method, customer_code,
--   source_phase_no, process_description, source_line_item_remarks, order_specification,
--   remarks, created_by, created_datetime, last_updated_by, last_updated_datetime,
--   last_modified_by, last_modified_datetime, object_version, customer_dn_no,
--   proposed_edd, mark_as_complete

SELECT *
FROM public.mfg_pp_vch;
