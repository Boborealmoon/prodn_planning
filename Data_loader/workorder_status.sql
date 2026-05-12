-- Workorder Status
-- Source: public.lg_out_shm_detail
--
-- Deduplicates by (item_code, item_desc, item_qty, source_voucher_line_item_no, source_voucher_no),
-- keeping the row with the highest status value (equivalent to sorting DESC then Table.Distinct).

SELECT DISTINCT ON (item_code, item_desc, item_qty, source_voucher_line_item_no, source_voucher_no)
    item_code,
    item_desc,
    item_qty,
    source_voucher_line_item_no,
    source_voucher_no,
    status
FROM public.lg_out_shm_detail
ORDER BY
    item_code,
    item_desc,
    item_qty,
    source_voucher_line_item_no,
    source_voucher_no,
    status DESC;
