PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS parts (
  part_id INTEGER PRIMARY KEY AUTOINCREMENT,
  part_name TEXT NOT NULL UNIQUE,
  part_desc TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bom_variation (
  bom_id INTEGER PRIMARY KEY AUTOINCREMENT,
  part_id INTEGER NOT NULL REFERENCES parts(part_id) ON DELETE CASCADE,
  flow_code TEXT NOT NULL,
  flow_name TEXT DEFAULT '',
  is_default INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS operation_seq (
  op_seq_id INTEGER PRIMARY KEY AUTOINCREMENT,
  bom_id INTEGER NOT NULL REFERENCES bom_variation(bom_id) ON DELETE CASCADE,
  seq_no INTEGER NOT NULL,
  op_no TEXT NOT NULL,
  op_type TEXT NOT NULL,
  machine_category TEXT NOT NULL,
  cycle_time REAL NOT NULL DEFAULT 1,
  setup_time REAL NOT NULL DEFAULT 0,
  preferred_machine TEXT DEFAULT '',
  is_last_op INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS machines (
  machine_id INTEGER PRIMARY KEY AUTOINCREMENT,
  machine_code TEXT NOT NULL UNIQUE,
  machine_category TEXT NOT NULL,
  shift_profile TEXT NOT NULL DEFAULT 'STANDARD',
  active INTEGER NOT NULL DEFAULT 1,
  notes TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS process_sheet (
  ps_id TEXT PRIMARY KEY,
  part_id INTEGER REFERENCES parts(part_id),
  inv_code TEXT DEFAULT '',
  inv_desc TEXT DEFAULT '',
  order_date TEXT DEFAULT '',
  due_date TEXT DEFAULT '',
  total_qty REAL NOT NULL DEFAULT 0,
  planned_qty REAL NOT NULL DEFAULT 0,
  finished_qty REAL NOT NULL DEFAULT 0,
  selected_bom_id INTEGER REFERENCES bom_variation(bom_id),
  planner_status TEXT NOT NULL DEFAULT 'UNPLANNED',
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS planning_block (
  block_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ps_id TEXT NOT NULL REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  flow_op_seq_id INTEGER REFERENCES operation_seq(op_seq_id),
  op_no TEXT NOT NULL,
  seq_no INTEGER NOT NULL,
  machine_id INTEGER NOT NULL REFERENCES machines(machine_id),
  machine_code TEXT NOT NULL,
  total_qty REAL NOT NULL DEFAULT 0,
  setup_charged INTEGER DEFAULT 0,
  locked INTEGER DEFAULT 0,
  archived INTEGER DEFAULT 0,
  envelope_start TEXT DEFAULT '',
  envelope_end TEXT DEFAULT '',
  status TEXT DEFAULT 'PLANNED',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS planning_row (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  envelope_id INTEGER NOT NULL DEFAULT 0,
  split_piece_id TEXT DEFAULT '',
  ps_id TEXT NOT NULL REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  plan_date TEXT NOT NULL,
  start_min INTEGER NOT NULL,
  end_min INTEGER NOT NULL,
  qty REAL NOT NULL DEFAULT 0,
  actual_out REAL NOT NULL DEFAULT 0,
  actual_out_set INTEGER NOT NULL DEFAULT 0,
  setup_mins REAL NOT NULL DEFAULT 0,
  locked INTEGER DEFAULT 0,
  seq_no INTEGER NOT NULL DEFAULT 0,
  op_no TEXT DEFAULT '',
  machine_id INTEGER NOT NULL DEFAULT 0,
  machine_code TEXT DEFAULT '',
  flow_op_seq_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS planning_envelope (
  envelope_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ps_id TEXT NOT NULL REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  seq_no INTEGER NOT NULL DEFAULT 0,
  op_no TEXT NOT NULL DEFAULT '',
  machine_id INTEGER NOT NULL DEFAULT 0,
  machine_code TEXT DEFAULT '',
  flow_op_seq_id INTEGER NOT NULL DEFAULT 0,
  split_piece_id TEXT DEFAULT '',
  total_qty REAL NOT NULL DEFAULT 0,
  locked INTEGER DEFAULT 0,
  archived INTEGER DEFAULT 0,
  envelope_start TEXT DEFAULT '',
  envelope_end TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (ps_id, seq_no, op_no, machine_id, machine_code, flow_op_seq_id, split_piece_id)
);

CREATE TABLE IF NOT EXISTS history_block (
  hist_block_id INTEGER PRIMARY KEY AUTOINCREMENT,
  original_block_id INTEGER,
  ps_id TEXT NOT NULL,
  op_no TEXT,
  seq_no INTEGER,
  machine_code TEXT,
  total_qty REAL DEFAULT 0,
  actual_out REAL DEFAULT 0,
  setup_charged INTEGER DEFAULT 0,
  envelope_start TEXT DEFAULT '',
  envelope_end TEXT DEFAULT '',
  archived_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS history_row (
  hist_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  hist_block_id INTEGER NOT NULL REFERENCES history_block(hist_block_id) ON DELETE CASCADE,
  hist_envelope_id INTEGER NOT NULL DEFAULT 0,
  ps_id TEXT NOT NULL,
  plan_date TEXT,
  start_min INTEGER,
  end_min INTEGER,
  qty REAL DEFAULT 0,
  actual_out REAL DEFAULT 0,
  actual_out_set INTEGER NOT NULL DEFAULT 0,
  seq_no INTEGER NOT NULL DEFAULT 0,
  op_no TEXT DEFAULT '',
  machine_id INTEGER NOT NULL DEFAULT 0,
  machine_code TEXT DEFAULT '',
  flow_op_seq_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS history_envelope (
  hist_envelope_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ps_id TEXT NOT NULL,
  seq_no INTEGER NOT NULL DEFAULT 0,
  op_no TEXT NOT NULL DEFAULT '',
  machine_id INTEGER NOT NULL DEFAULT 0,
  machine_code TEXT DEFAULT '',
  flow_op_seq_id INTEGER NOT NULL DEFAULT 0,
  total_qty REAL NOT NULL DEFAULT 0,
  actual_out REAL DEFAULT 0,
  locked INTEGER DEFAULT 0,
  archived_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (ps_id, seq_no, op_no, machine_id, machine_code, flow_op_seq_id)
);

CREATE TABLE IF NOT EXISTS process_sheet_material (
  mat_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ps_id TEXT NOT NULL UNIQUE REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  material_name TEXT DEFAULT '',
  material_ready INTEGER DEFAULT 0,
  material_ready_qty REAL DEFAULT 0,
  need_by_date TEXT DEFAULT '',
  order_status TEXT DEFAULT 'TO_ORDER',
  planner_note TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS process_sheet_material_order_log (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT,
  mat_id INTEGER NOT NULL REFERENCES process_sheet_material(mat_id) ON DELETE CASCADE,
  ps_id TEXT NOT NULL REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  ordered_qty REAL DEFAULT 0,
  received_qty REAL DEFAULT 0,
  order_date TEXT DEFAULT '',
  expected_date TEXT DEFAULT '',
  received_date TEXT DEFAULT '',
  log_status TEXT DEFAULT 'PENDING',
  note TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS process_sheet_support (
  support_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ps_id TEXT NOT NULL REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  support_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING',
  need_by_date TEXT DEFAULT '',
  promised_date TEXT DEFAULT '',
  ready_date TEXT DEFAULT '',
  note TEXT DEFAULT '',
  updated_by TEXT DEFAULT '',
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (ps_id, support_type)
);

CREATE TABLE IF NOT EXISTS process_sheet_manual_actual (
  ps_id TEXT PRIMARY KEY REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  actual_qty REAL NOT NULL DEFAULT 0,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS process_sheet_erp_link (
  ps_id TEXT PRIMARY KEY REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  pp_partial_no TEXT DEFAULT '',
  bom_code TEXT DEFAULT '',
  erp_status TEXT DEFAULT '',
  last_sync_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS process_sheet_erp_actual (
  ps_id TEXT PRIMARY KEY REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  actual_qty REAL NOT NULL DEFAULT 0,
  reject_qty REAL NOT NULL DEFAULT 0,
  source_sheet TEXT DEFAULT 'Workorder Tracker',
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS process_sheet_local_override (
  ps_id TEXT PRIMARY KEY REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  force_completed INTEGER NOT NULL DEFAULT 0,
  planner_status_override TEXT DEFAULT '',
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS process_sheet_erp_sync_lock (
  ps_id TEXT PRIMARY KEY REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  locked INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS part_flow_local_override (
  bom_id INTEGER PRIMARY KEY REFERENCES bom_variation(bom_id) ON DELETE CASCADE,
  erp_sync_locked INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS process_sheet_step_local_override (
  ps_id TEXT NOT NULL REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  op_seq_id INTEGER NOT NULL REFERENCES operation_seq(op_seq_id) ON DELETE CASCADE,
  force_completed INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (ps_id, op_seq_id)
);

CREATE TABLE IF NOT EXISTS process_sheet_progress_snapshot (
  snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ps_id TEXT NOT NULL REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  reason TEXT NOT NULL DEFAULT 'MANUAL_RESET',
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS erp_process_sheets_staging (
  stage_id INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_batch_id TEXT NOT NULL,
  ps_id TEXT DEFAULT '',
  pp_partial_no TEXT DEFAULT '',
  part_no TEXT DEFAULT '',
  description TEXT DEFAULT '',
  total_qty REAL DEFAULT 0,
  partial_qty REAL DEFAULT 0,
  due_date TEXT DEFAULT '',
  order_date TEXT DEFAULT '',
  bom_code TEXT DEFAULT '',
  status TEXT DEFAULT '',
  raw_payload TEXT DEFAULT '',
  imported_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS erp_materials_per_ps_staging (
  stage_id INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_batch_id TEXT NOT NULL,
  pp_voucher TEXT DEFAULT '',
  pp_partial_no TEXT DEFAULT '',
  inventory_code TEXT DEFAULT '',
  material_inventory_code TEXT DEFAULT '',
  raw_payload TEXT DEFAULT '',
  imported_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS erp_materials_per_bom_staging (
  stage_id INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_batch_id TEXT NOT NULL,
  bom_code TEXT DEFAULT '',
  source_inventory_code TEXT DEFAULT '',
  material_inventory_code TEXT DEFAULT '',
  description TEXT DEFAULT '',
  raw_payload TEXT DEFAULT '',
  imported_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS erp_bom_op_stage_staging (
  stage_id INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_batch_id TEXT NOT NULL,
  bom_code TEXT DEFAULT '',
  inventory_code TEXT DEFAULT '',
  seq_index INTEGER DEFAULT 0,
  stage_no TEXT DEFAULT '',
  stage_desc TEXT DEFAULT '',
  machine_no TEXT DEFAULT '',
  machine_category TEXT DEFAULT '',
  cycle_time REAL DEFAULT 0,
  setup_time REAL DEFAULT 0,
  raw_payload TEXT DEFAULT '',
  imported_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS erp_workorder_tracker_staging (
  stage_id INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_batch_id TEXT NOT NULL,
  voucher_no TEXT DEFAULT '',
  source_pp_no TEXT DEFAULT '',
  inventory_code TEXT DEFAULT '',
  machine_no TEXT DEFAULT '',
  partial_seq_no TEXT DEFAULT '',
  stage_no TEXT DEFAULT '',
  stage_desc TEXT DEFAULT '',
  acc_completion_qty REAL DEFAULT 0,
  rej_completion_qty REAL DEFAULT 0,
  total_acc_qty_produced REAL DEFAULT 0,
  total_rej_qty_produced REAL DEFAULT 0,
  employee_name TEXT DEFAULT '',
  bom_code TEXT DEFAULT '',
  raw_payload TEXT DEFAULT '',
  imported_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS erp_active_orders_staging (
  stage_id INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_batch_id TEXT NOT NULL,
  ps_id TEXT DEFAULT '',
  part_no TEXT DEFAULT '',
  bom_code TEXT DEFAULT '',
  raw_payload TEXT DEFAULT '',
  imported_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS erp_sync_log (
  sync_id INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_batch_id TEXT NOT NULL UNIQUE,
  source_name TEXT DEFAULT '',
  workbook_name TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'STARTED',
  message TEXT DEFAULT '',
  inserted_count INTEGER DEFAULT 0,
  updated_count INTEGER DEFAULT 0,
  skipped_count INTEGER DEFAULT 0,
  started_at TEXT DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS staff (
  staff_id INTEGER PRIMARY KEY AUTOINCREMENT,
  staff_name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'MACHINIST',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS machine_staff_assignment (
  assign_id INTEGER PRIMARY KEY AUTOINCREMENT,
  machine_id INTEGER NOT NULL REFERENCES machines(machine_id) ON DELETE CASCADE,
  staff_id INTEGER NOT NULL REFERENCES staff(staff_id) ON DELETE CASCADE,
  assign_date TEXT NOT NULL,
  shift TEXT DEFAULT 'DAY',
  UNIQUE(machine_id, staff_id, assign_date, shift)
);

CREATE TABLE IF NOT EXISTS calendar_days (
  work_date TEXT PRIMARY KEY,
  is_working_day INTEGER NOT NULL DEFAULT 1,
  note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS capacity_profile (
  profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_name TEXT NOT NULL UNIQUE,
  capacity_minutes INTEGER NOT NULL DEFAULT 0,
  start_minute INTEGER NOT NULL DEFAULT 510,
  note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS machine_capacity_day (
  day_id INTEGER PRIMARY KEY AUTOINCREMENT,
  machine_id INTEGER NOT NULL REFERENCES machines(machine_id) ON DELETE CASCADE,
  work_date TEXT NOT NULL,
  profile_id INTEGER NOT NULL REFERENCES capacity_profile(profile_id),
  capacity_minutes INTEGER NOT NULL DEFAULT 0,
  start_minute INTEGER NOT NULL DEFAULT 510,
  note TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(machine_id, work_date)
);

CREATE TABLE IF NOT EXISTS trial_operation (
  operation_id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_no TEXT NOT NULL,
  operation_name TEXT NOT NULL,
  total_qty REAL NOT NULL DEFAULT 0,
  setup_minutes REAL NOT NULL DEFAULT 0,
  cycle_minutes_per_qty REAL NOT NULL DEFAULT 0,
  compatible_machine_group TEXT DEFAULT '',
  source_ps_id TEXT DEFAULT '',
  source_op_seq_id INTEGER NOT NULL DEFAULT 0,
  source_op_no TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  remarks TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trial_run_block (
  block_id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_id INTEGER NOT NULL REFERENCES trial_operation(operation_id) ON DELETE CASCADE,
  machine_id INTEGER NOT NULL REFERENCES machines(machine_id),
  queue_position INTEGER NOT NULL DEFAULT 0,
  scheduled_qty REAL NOT NULL DEFAULT 0,
  include_setup INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'PLANNED',
  anchor_datetime TEXT DEFAULT '',
  calculated_start_datetime TEXT DEFAULT '',
  calculated_end_datetime TEXT DEFAULT '',
  actual_good_qty REAL NOT NULL DEFAULT 0,
  actual_reject_qty REAL NOT NULL DEFAULT 0,
  remarks TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trial_run_block_segment (
  segment_id INTEGER PRIMARY KEY AUTOINCREMENT,
  block_id INTEGER NOT NULL REFERENCES trial_run_block(block_id) ON DELETE CASCADE,
  machine_id INTEGER NOT NULL REFERENCES machines(machine_id),
  segment_date TEXT NOT NULL,
  segment_type TEXT NOT NULL,
  qty_done REAL NOT NULL DEFAULT 0,
  minutes_used REAL NOT NULL DEFAULT 0,
  start_datetime TEXT NOT NULL,
  end_datetime TEXT NOT NULL,
  is_actual INTEGER NOT NULL DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trial_production_actual (
  actual_id INTEGER PRIMARY KEY AUTOINCREMENT,
  segment_id INTEGER,
  block_id INTEGER NOT NULL REFERENCES trial_run_block(block_id) ON DELETE CASCADE,
  report_date TEXT NOT NULL,
  output_qty REAL DEFAULT NULL,
  reject_qty REAL DEFAULT NULL,
  target_qty_at_report REAL DEFAULT NULL,
  remarks TEXT DEFAULT '',
  reported_by TEXT DEFAULT '',
  reported_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(block_id, report_date)
);

CREATE VIEW IF NOT EXISTS flows AS
SELECT bom_id, part_id, flow_code, flow_name, is_default FROM bom_variation;

CREATE VIEW IF NOT EXISTS flow_steps AS
SELECT op_seq_id, bom_id, seq_no AS seq, op_no, op_type, machine_category,
       preferred_machine, cycle_time, setup_time, is_last_op
FROM operation_seq;

CREATE VIEW IF NOT EXISTS process_sheets AS
SELECT ps_id, part_id, selected_bom_id, inv_code, inv_desc, order_date, due_date,
       total_qty, status, planned_qty, finished_qty, planner_status
FROM process_sheet;

CREATE VIEW IF NOT EXISTS materials AS
SELECT mat_id, ps_id, material_name, material_ready, material_ready_qty,
       order_status, need_by_date, planner_note
FROM process_sheet_material;

CREATE VIEW IF NOT EXISTS material_order_logs AS
SELECT log_id, mat_id, ordered_qty, received_qty, order_date, expected_date, log_status, note
FROM process_sheet_material_order_log;

CREATE VIEW IF NOT EXISTS planning_blocks AS
SELECT block_id, ps_id, flow_op_seq_id AS op_seq_id, machine_id, locked, archived
FROM planning_block;

CREATE VIEW IF NOT EXISTS planning_rows AS
SELECT row_id, block_id, envelope_id, ps_id, plan_date, start_min, end_min, qty, actual_out, actual_out_set, setup_mins,
       locked, seq_no, op_no, machine_id, machine_code, flow_op_seq_id
FROM planning_row;

CREATE VIEW IF NOT EXISTS planning_envelopes AS
SELECT envelope_id, ps_id, seq_no, op_no, machine_id, machine_code, flow_op_seq_id,
       total_qty, locked, archived, envelope_start, envelope_end, created_at, updated_at
FROM planning_envelope;

CREATE VIEW IF NOT EXISTS staffing_assignments AS
SELECT assign_id, machine_id, staff_id, assign_date, shift
FROM machine_staff_assignment;

CREATE VIEW IF NOT EXISTS history AS
SELECT hb.hist_block_id AS hist_id, hb.archived_at, hb.ps_id, p.part_name, hb.op_no,
       hb.machine_code, hr.plan_date, hr.start_min, hr.end_min,
       hr.qty AS row_qty, hr.actual_out AS row_actual
FROM history_block hb
JOIN history_row hr ON hr.hist_block_id = hb.hist_block_id
LEFT JOIN process_sheet ps ON ps.ps_id = hb.ps_id
LEFT JOIN parts p ON p.part_id = ps.part_id;

CREATE VIEW IF NOT EXISTS history_envelopes AS
SELECT hist_envelope_id, ps_id, seq_no, op_no, machine_id, machine_code, flow_op_seq_id,
       total_qty, actual_out, locked, archived_at
FROM history_envelope;

CREATE TRIGGER IF NOT EXISTS flows_insert INSTEAD OF INSERT ON flows
BEGIN
  INSERT INTO bom_variation (part_id, flow_code, flow_name, is_default)
  VALUES (NEW.part_id, NEW.flow_code, NEW.flow_name, COALESCE(NEW.is_default, 0));
END;

CREATE TRIGGER IF NOT EXISTS flows_update INSTEAD OF UPDATE ON flows
BEGIN
  UPDATE bom_variation
  SET flow_code = NEW.flow_code, flow_name = NEW.flow_name, is_default = COALESCE(NEW.is_default, 0)
  WHERE bom_id = OLD.bom_id;
END;

CREATE TRIGGER IF NOT EXISTS flow_steps_insert INSTEAD OF INSERT ON flow_steps
BEGIN
  INSERT INTO operation_seq (bom_id, seq_no, op_no, op_type, machine_category, preferred_machine, cycle_time, setup_time, is_last_op)
  VALUES (NEW.bom_id, NEW.seq, NEW.op_no, NEW.op_type, NEW.machine_category, NEW.preferred_machine, NEW.cycle_time, NEW.setup_time, COALESCE(NEW.is_last_op, 0));
END;

CREATE TRIGGER IF NOT EXISTS flow_steps_delete INSTEAD OF DELETE ON flow_steps
BEGIN
  DELETE FROM operation_seq WHERE op_seq_id = OLD.op_seq_id;
END;

CREATE TRIGGER IF NOT EXISTS process_sheets_insert INSTEAD OF INSERT ON process_sheets
BEGIN
  INSERT INTO process_sheet (ps_id, part_id, selected_bom_id, inv_code, inv_desc, order_date, due_date, total_qty, status)
  VALUES (NEW.ps_id, NEW.part_id, NEW.selected_bom_id, NEW.inv_code, NEW.inv_desc, NEW.order_date, NEW.due_date, NEW.total_qty, COALESCE(NEW.status, 'ACTIVE'));
END;

CREATE TRIGGER IF NOT EXISTS process_sheets_update INSTEAD OF UPDATE ON process_sheets
BEGIN
  UPDATE process_sheet
  SET inv_code = NEW.inv_code, inv_desc = NEW.inv_desc, order_date = NEW.order_date,
      due_date = NEW.due_date, total_qty = NEW.total_qty, status = NEW.status,
      selected_bom_id = NEW.selected_bom_id, updated_at = CURRENT_TIMESTAMP
  WHERE ps_id = OLD.ps_id;
END;

CREATE TRIGGER IF NOT EXISTS materials_insert INSTEAD OF INSERT ON materials
BEGIN
  INSERT INTO process_sheet_material (ps_id, material_name, material_ready, material_ready_qty, order_status, need_by_date, planner_note)
  VALUES (NEW.ps_id, NEW.material_name, COALESCE(NEW.material_ready, 0), COALESCE(NEW.material_ready_qty, 0), COALESCE(NEW.order_status, 'TO_ORDER'), NEW.need_by_date, NEW.planner_note);
END;

CREATE TRIGGER IF NOT EXISTS materials_update INSTEAD OF UPDATE ON materials
BEGIN
  UPDATE process_sheet_material
  SET material_name = NEW.material_name, material_ready = NEW.material_ready,
      material_ready_qty = NEW.material_ready_qty, order_status = NEW.order_status,
      need_by_date = NEW.need_by_date, planner_note = NEW.planner_note,
      updated_at = CURRENT_TIMESTAMP
  WHERE mat_id = OLD.mat_id;
END;

CREATE TRIGGER IF NOT EXISTS material_order_logs_insert INSTEAD OF INSERT ON material_order_logs
BEGIN
  INSERT INTO process_sheet_material_order_log (mat_id, ps_id, ordered_qty, received_qty, order_date, expected_date, log_status, note)
  SELECT NEW.mat_id, ps_id, COALESCE(NEW.ordered_qty, 0), COALESCE(NEW.received_qty, 0),
         NEW.order_date, NEW.expected_date, COALESCE(NEW.log_status, 'PENDING'), NEW.note
  FROM process_sheet_material WHERE mat_id = NEW.mat_id;
END;

CREATE TRIGGER IF NOT EXISTS planning_blocks_insert INSTEAD OF INSERT ON planning_blocks
BEGIN
  INSERT INTO planning_block (ps_id, flow_op_seq_id, op_no, seq_no, machine_id, machine_code, total_qty, locked, archived)
  SELECT NEW.ps_id, NEW.op_seq_id, fs.op_no, fs.seq_no, NEW.machine_id, m.machine_code, 0, COALESCE(NEW.locked, 0), COALESCE(NEW.archived, 0)
  FROM operation_seq fs, machines m
  WHERE fs.op_seq_id = NEW.op_seq_id AND m.machine_id = NEW.machine_id;
  INSERT OR IGNORE INTO planning_envelope (ps_id, seq_no, op_no, machine_id, machine_code, flow_op_seq_id, total_qty, locked, archived, envelope_start, envelope_end)
  SELECT NEW.ps_id, fs.seq_no, fs.op_no, NEW.machine_id, m.machine_code, NEW.op_seq_id, 0, COALESCE(NEW.locked, 0), COALESCE(NEW.archived, 0), '', ''
  FROM operation_seq fs, machines m
  WHERE fs.op_seq_id = NEW.op_seq_id AND m.machine_id = NEW.machine_id;
END;

CREATE TRIGGER IF NOT EXISTS planning_blocks_update INSTEAD OF UPDATE ON planning_blocks
BEGIN
  UPDATE planning_block
  SET machine_id = COALESCE(NEW.machine_id, machine_id),
      machine_code = COALESCE((SELECT machine_code FROM machines WHERE machine_id = NEW.machine_id), machine_code),
      locked = COALESCE(NEW.locked, locked),
      archived = COALESCE(NEW.archived, archived)
  WHERE block_id = OLD.block_id;
END;

CREATE TRIGGER IF NOT EXISTS planning_blocks_delete INSTEAD OF DELETE ON planning_blocks
BEGIN
  DELETE FROM planning_block WHERE block_id = OLD.block_id;
END;

CREATE TRIGGER IF NOT EXISTS planning_rows_insert INSTEAD OF INSERT ON planning_rows
BEGIN
  INSERT INTO planning_row (block_id, envelope_id, split_piece_id, ps_id, plan_date, start_min, end_min, qty, actual_out, actual_out_set, setup_mins, seq_no, op_no, machine_id, machine_code, flow_op_seq_id)
  SELECT
      NEW.block_id,
      COALESCE((
          SELECT pe.envelope_id
          FROM planning_envelope pe
          WHERE pe.ps_id = pb.ps_id
            AND pe.seq_no = pb.seq_no
            AND pe.op_no = pb.op_no
            AND pe.machine_id = pb.machine_id
            AND pe.machine_code = pb.machine_code
            AND pe.flow_op_seq_id = pb.flow_op_seq_id
          LIMIT 1
      ), 0),
      COALESCE((
          SELECT pe.split_piece_id
          FROM planning_envelope pe
          JOIN planning_block pb ON pb.ps_id = pe.ps_id
                              AND pb.seq_no = pe.seq_no
                              AND pb.op_no = pe.op_no
                              AND pb.machine_id = pe.machine_id
                              AND pb.machine_code = pe.machine_code
                              AND pb.flow_op_seq_id = pe.flow_op_seq_id
          WHERE pb.block_id = NEW.block_id
          LIMIT 1
      ), ''),
      pb.ps_id,
      NEW.plan_date,
      NEW.start_min,
      NEW.end_min,
      NEW.qty,
      COALESCE(NEW.actual_out, 0),
      CASE WHEN NEW.actual_out IS NULL THEN 0 ELSE 1 END,
      COALESCE(NEW.setup_mins, 0),
      pb.seq_no,
      pb.op_no,
      pb.machine_id,
      pb.machine_code,
      pb.flow_op_seq_id
  FROM planning_block pb
  WHERE pb.block_id = NEW.block_id;
  UPDATE planning_block
  SET total_qty = (SELECT COALESCE(SUM(qty), 0) FROM planning_row WHERE block_id = NEW.block_id),
      envelope_start = (SELECT MIN(plan_date || ' ' || printf('%02d:%02d', start_min / 60, start_min % 60)) FROM planning_row WHERE block_id = NEW.block_id),
      envelope_end = (SELECT MAX(plan_date || ' ' || printf('%02d:%02d', end_min / 60, end_min % 60)) FROM planning_row WHERE block_id = NEW.block_id)
  WHERE block_id = NEW.block_id;
  UPDATE planning_envelope
  SET total_qty = (SELECT COALESCE(SUM(qty), 0) FROM planning_row WHERE block_id = NEW.block_id),
      envelope_start = (SELECT MIN(plan_date || ' ' || printf('%02d:%02d', start_min / 60, start_min % 60)) FROM planning_row WHERE block_id = NEW.block_id),
      envelope_end = (SELECT MAX(plan_date || ' ' || printf('%02d:%02d', end_min / 60, end_min % 60)) FROM planning_row WHERE block_id = NEW.block_id),
      updated_at = CURRENT_TIMESTAMP
  WHERE envelope_id = (
      SELECT pe.envelope_id
      FROM planning_envelope pe
      JOIN planning_block pb ON pb.ps_id = pe.ps_id
                          AND pb.seq_no = pe.seq_no
                          AND pb.op_no = pe.op_no
                          AND pb.machine_id = pe.machine_id
                          AND pb.machine_code = pe.machine_code
                          AND pb.flow_op_seq_id = pe.flow_op_seq_id
      WHERE pb.block_id = NEW.block_id
      LIMIT 1
  );
END;

CREATE TRIGGER IF NOT EXISTS planning_rows_update INSTEAD OF UPDATE ON planning_rows
BEGIN
  UPDATE planning_row
  SET plan_date = COALESCE(NEW.plan_date, plan_date), start_min = COALESCE(NEW.start_min, start_min),
      end_min = COALESCE(NEW.end_min, end_min), qty = COALESCE(NEW.qty, qty),
      actual_out = COALESCE(NEW.actual_out, actual_out),
      actual_out_set = CASE WHEN NEW.actual_out IS NULL THEN actual_out_set ELSE 1 END,
      setup_mins = COALESCE(NEW.setup_mins, setup_mins)
  WHERE row_id = OLD.row_id;
  UPDATE planning_block
  SET total_qty = (SELECT COALESCE(SUM(qty), 0) FROM planning_row WHERE block_id = OLD.block_id),
      envelope_start = (SELECT MIN(plan_date || ' ' || printf('%02d:%02d', start_min / 60, start_min % 60)) FROM planning_row WHERE block_id = OLD.block_id),
      envelope_end = (SELECT MAX(plan_date || ' ' || printf('%02d:%02d', end_min / 60, end_min % 60)) FROM planning_row WHERE block_id = OLD.block_id)
  WHERE block_id = OLD.block_id;
  UPDATE planning_envelope
  SET total_qty = (SELECT COALESCE(SUM(qty), 0) FROM planning_row WHERE block_id = OLD.block_id),
      envelope_start = (SELECT MIN(plan_date || ' ' || printf('%02d:%02d', start_min / 60, start_min % 60)) FROM planning_row WHERE block_id = OLD.block_id),
      envelope_end = (SELECT MAX(plan_date || ' ' || printf('%02d:%02d', end_min / 60, end_min % 60)) FROM planning_row WHERE block_id = OLD.block_id),
      updated_at = CURRENT_TIMESTAMP
  WHERE envelope_id = (SELECT envelope_id FROM planning_row WHERE row_id = OLD.row_id);
END;

CREATE TRIGGER IF NOT EXISTS staffing_assignments_insert INSTEAD OF INSERT ON staffing_assignments
BEGIN
  INSERT OR IGNORE INTO machine_staff_assignment (machine_id, staff_id, assign_date, shift)
  VALUES (NEW.machine_id, NEW.staff_id, NEW.assign_date, COALESCE(NEW.shift, 'DAY'));
END;

CREATE TRIGGER IF NOT EXISTS staffing_assignments_delete INSTEAD OF DELETE ON staffing_assignments
BEGIN
  DELETE FROM machine_staff_assignment WHERE assign_id = OLD.assign_id;
END;

CREATE TRIGGER IF NOT EXISTS history_insert INSTEAD OF INSERT ON history
BEGIN
  INSERT INTO history_block (ps_id, op_no, machine_code, total_qty, actual_out)
  VALUES (NEW.ps_id, NEW.op_no, NEW.machine_code, NEW.row_qty, NEW.row_actual);
  INSERT INTO history_envelope (ps_id, seq_no, op_no, machine_id, machine_code, flow_op_seq_id, total_qty, actual_out, locked, archived_at)
  VALUES (NEW.ps_id, 0, NEW.op_no, 0, NEW.machine_code, 0, NEW.row_qty, NEW.row_actual, 0, CURRENT_TIMESTAMP);
  INSERT INTO history_row (hist_block_id, hist_envelope_id, ps_id, plan_date, start_min, end_min, qty, actual_out, actual_out_set, seq_no, op_no, machine_id, machine_code, flow_op_seq_id)
  VALUES (
      (SELECT hist_block_id FROM history_block ORDER BY hist_block_id DESC LIMIT 1),
      (SELECT hist_envelope_id FROM history_envelope ORDER BY hist_envelope_id DESC LIMIT 1),
      NEW.ps_id, NEW.plan_date, NEW.start_min, NEW.end_min, NEW.row_qty, NEW.row_actual,
      CASE WHEN NEW.row_actual IS NULL THEN 0 ELSE 1 END, 0, NEW.op_no, 0, NEW.machine_code, 0
  );
END;
