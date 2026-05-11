-- Fetch process sheets (work orders) from the ERP.
-- Required output columns:
--   ps_id            TEXT  -- unique process sheet / PP number
--   pp_partial_no    TEXT  -- partial/sequence number
--   part_no          TEXT  -- inventory / part code
--   description      TEXT  -- part description
--   total_qty        NUM   -- total order quantity
--   partial_qty      NUM   -- partial quantity (0 if not used)
--   due_date         DATE  -- required completion date
--   order_date       DATE  -- order creation date
--   bom_code         TEXT  -- BOM reference code
--   status           TEXT  -- order status (e.g. ACTIVE, COMPLETED)
--
-- Replace <process_sheets_table> with the actual ERP table name.
-- Use AS aliases to map ERP column names to the expected names above.

SELECT
    ps_id,
    pp_partial_no,
    part_no,
    description,
    total_qty,
    partial_qty,
    due_date,
    order_date,
    bom_code,
    status
FROM <process_sheets_table>
WHERE status NOT IN ('CANCELLED', 'DELETED');
