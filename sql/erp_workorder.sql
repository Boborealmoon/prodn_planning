-- Fetch workorder tracker (actuals / completion records) from the ERP.
-- Required output columns:
--   voucher_no                TEXT  -- workorder voucher number
--   source_pp_no              TEXT  -- source PP / process sheet number
--   inventory_code            TEXT  -- part inventory code
--   machine_no                TEXT  -- machine used
--   partial_seq_no            TEXT  -- partial sequence number
--   stage_no                  TEXT  -- operation/stage number
--   stage_desc                TEXT  -- operation description
--   acc_completion_qty        NUM   -- accepted completion quantity (this entry)
--   rej_completion_qty        NUM   -- rejected completion quantity (this entry)
--   total_acc_qty_produced    NUM   -- cumulative accepted quantity
--   total_rej_qty_produced    NUM   -- cumulative rejected quantity
--   employee_name             TEXT  -- operator / employee name
--   bom_code                  TEXT  -- BOM reference code
--
-- Replace <workorder_table> with the actual ERP table name.

SELECT
    voucher_no,
    source_pp_no,
    inventory_code,
    machine_no,
    partial_seq_no,
    stage_no,
    stage_desc,
    acc_completion_qty,
    rej_completion_qty,
    total_acc_qty_produced,
    total_rej_qty_produced,
    employee_name,
    bom_code
FROM <workorder_table>;
