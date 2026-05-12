-- Workorder Tracker
-- Sources:
--   mfg_wo_comp_vch   (t1) -- completion voucher entries
--   mfg_mps_vch       (t2) -- MPS voucher lines (stage/step detail)
--   mfg_wo_vch        (t3) -- workorder header (cumulative qty)
--   mt_employee       (t4) -- employee master
--   public.mfg_pp_vch          -- PP voucher table; provides bom_code by pp_voucher_no
--
-- Deduplicates to one row per voucher_no:
--   picks the entry with the highest total_acc_qty_produced,
--   preferring 'H' (Done) over 'D' (Ongoing) status when tied.
--
-- Final columns:
--   source_pp_no, inventory_code, voucher_no, machine_no, partial_seq_no,
--   stage_no, stage_desc, acc_completion_qty, rej_completion_qty,
--   total_acc_qty_produced, total_rej_qty_produced, employee_name, bom_code

WITH raw_joined AS (
    SELECT
        t2.source_pp_no,
        t2.inventory_code,
        t1.voucher_no,
        t1.machine_no,
        t1.partial_seq_no,
        t2.stage_no,
        t2.stage_desc,
        t1.acc_completion_qty,
        t1.rej_completion_qty,
        t3.total_acc_qty_produced,
        t3.total_rej_qty_produced,
        t4.employee_name,
        t1.status AS raw_status
    FROM mfg_wo_comp_vch t1
    LEFT JOIN mfg_mps_vch t2
        ON  t1.voucher_no = t2.wo_voucher_no
    LEFT JOIN mfg_wo_vch t3
        ON  t1.voucher_no = t3.voucher_no
    LEFT JOIN mt_employee t4
        ON  t1.employee_code = t4.employee_code
),

with_bom AS (
    SELECT
        r.*,
        p.bom_code
    FROM raw_joined r
    LEFT JOIN public.mfg_pp_vch p
        ON  p.pp_voucher_no = r.source_pp_no
),

machining_only AS (
    SELECT *
    FROM with_bom
    WHERE stage_desc LIKE 'Turning%'
       OR stage_desc LIKE 'Milling%'
       OR stage_desc LIKE 'Turnmill%'
),

ranked AS (
    -- Top row per voucher_no: highest cumulative qty produced, Done ('H') preferred over Ongoing ('D')
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY voucher_no
            ORDER BY
                total_acc_qty_produced DESC,
                CASE WHEN raw_status = 'H' THEN 1 ELSE 0 END DESC
        ) AS rn
    FROM machining_only
)

SELECT
    source_pp_no,
    inventory_code,
    voucher_no,
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
FROM ranked
WHERE rn = 1
ORDER BY voucher_no DESC;
