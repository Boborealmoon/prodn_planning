-- BOM Operations (Routing Steps)
-- Sources:
--   public.mt_inventory_bom_stage         -- BOM stage/routing table
--   mfg_wo_comp_vch / mfg_mps_vch /       -- workorder tracker (embedded below)
--   mfg_wo_vch / mt_employee
--   public.mfg_pp_vch                     -- PP voucher table; provides bom_code by pp_voucher_no
--
-- NOTE: The final join for "Index" and "setup_time" is done in Power Query
--       against the Google Sheets lookup (part_no + Op No.) and cannot be expressed in SQL.
--
-- Output columns from this query:
--   inventory_code, bom_code, stage_no, stage_desc, op_no, machine_no

-- ── Workorder Tracker (deduplicated) ────────────────────────────────────────
WITH wt_raw AS (
    SELECT
        t2.source_pp_no,
        t2.inventory_code,
        t1.voucher_no,
        t1.machine_no,
        t2.stage_desc,
        t3.total_acc_qty_produced,
        t1.status AS raw_status
    FROM mfg_wo_comp_vch t1
    LEFT JOIN mfg_mps_vch t2
        ON  t1.voucher_no = t2.wo_voucher_no
    LEFT JOIN mfg_wo_vch t3
        ON  t1.voucher_no = t3.voucher_no
    WHERE t2.stage_desc LIKE 'Turning%'
       OR t2.stage_desc LIKE 'Milling%'
       OR t2.stage_desc LIKE 'Turnmill%'
),

wt_with_bom AS (
    SELECT
        r.*,
        p.bom_code
    FROM wt_raw r
    LEFT JOIN public.mfg_pp_vch p
        ON  p.pp_voucher_no = r.source_pp_no
),

wt_ranked AS (
    -- One row per voucher_no: highest total_acc_qty_produced, Done ('H') preferred
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY voucher_no
            ORDER BY
                total_acc_qty_produced DESC,
                CASE WHEN raw_status = 'H' THEN 1 ELSE 0 END DESC
        ) AS rn
    FROM wt_with_bom
),

workorder_tracker AS (
    SELECT source_pp_no, inventory_code, voucher_no, machine_no, stage_desc, bom_code
    FROM wt_ranked
    WHERE rn = 1
),

-- ── Workorder Tracker Slim (one machine_no per stage) ───────────────────────
workorder_tracker_slim AS (
    -- Collapse to one row per (inventory_code, bom_code, stage_desc),
    -- picking the first non-null machine_no (MIN ignores NULLs)
    SELECT
        inventory_code,
        bom_code,
        stage_desc,
        MIN(machine_no) AS machine_no
    FROM workorder_tracker
    GROUP BY inventory_code, bom_code, stage_desc
),

-- ── BOM Machining Stages ─────────────────────────────────────────────────────
bom_machining AS (
    SELECT
        inventory_code,
        bom_code,
        stage_no,
        stage_desc,
        CASE
            WHEN SPLIT_PART(stage_desc, ' ', 2) ~ '^\d+$'
            THEN SPLIT_PART(stage_desc, ' ', 2)::INTEGER
            ELSE NULL
        END AS op_no
    FROM public.mt_inventory_bom_stage
    WHERE stage_desc IS NOT NULL
      AND (
          stage_desc LIKE 'Turning%'
       OR stage_desc LIKE 'Milling%'
       OR stage_desc LIKE 'Turnmill%'
      )
)

-- ── Final Join ───────────────────────────────────────────────────────────────
SELECT
    b.inventory_code,
    b.bom_code,
    b.stage_no,
    b.stage_desc,
    b.op_no,
    w.machine_no
FROM bom_machining b
LEFT JOIN workorder_tracker_slim w
    ON  w.inventory_code = b.inventory_code
    AND w.bom_code       = b.bom_code
    AND w.stage_desc     = b.stage_desc
ORDER BY
    b.inventory_code ASC,
    b.bom_code       ASC,
    b.op_no          ASC;
