-- Fetch BOM operation stages (routing steps) from the ERP.
-- Required output columns:
--   bom_code         TEXT  -- BOM reference code
--   inventory_code   TEXT  -- part inventory code (also accepted: part_no, item_code)
--   index            NUM   -- sequence index within the BOM
--   stage_no         TEXT  -- operation/stage number (also accepted: op_no)
--   stage_desc       TEXT  -- operation description
--   machine_no       TEXT  -- preferred machine code (also accepted: preferred_machine, machine_code, machine)
--   cycle_time       NUM   -- cycle time in minutes
--   setup_time       NUM   -- setup time in minutes
--
-- Replace <bom_ops_table> with the actual ERP table name.

SELECT
    bom_code,
    inventory_code,
    seq_index   AS index,
    stage_no,
    stage_desc,
    machine_no,
    cycle_time,
    setup_time
FROM <bom_ops_table>
ORDER BY bom_code, seq_index;
