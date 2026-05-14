PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS calendar_days (
  work_date TEXT PRIMARY KEY,
  is_working_day INTEGER NOT NULL DEFAULT 1,
  note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS data_import_log (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT,
  import_type TEXT NOT NULL,
  workbook_name TEXT DEFAULT '',
  active_sheet_name TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'STARTED',
  message TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS machines (
  machine_id INTEGER PRIMARY KEY AUTOINCREMENT, -- to be removed and replace the and change the key to machine_code. Change cascading foreign keys.
  machine_code TEXT NOT NULL UNIQUE,
  machine_category TEXT NOT NULL,
  shift_profile TEXT NOT NULL DEFAULT 'STANDARD',
  active INTEGER NOT NULL DEFAULT 1,
  notes TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS parts (
  part_id INTEGER PRIMARY KEY AUTOINCREMENT,
  part_no TEXT NOT NULL UNIQUE,
  part_desc TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bom_variation (
  bom_id INTEGER PRIMARY KEY AUTOINCREMENT,
  part_id INTEGER NOT NULL REFERENCES parts(part_id) ON DELETE CASCADE,
  bom_code TEXT NOT NULL,
  bom_desc TEXT DEFAULT '',
  is_default INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(part_id, bom_code)
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
  is_last_op INTEGER DEFAULT 0,
  UNIQUE(bom_id, seq_no, op_no)
);

CREATE TABLE IF NOT EXISTS process_sheet (
  ps_id TEXT PRIMARY KEY,
  part_id INTEGER REFERENCES parts(part_id),
  part_no TEXT DEFAULT '',
  part_desc TEXT DEFAULT '',
  order_date TEXT DEFAULT '',
  due_date TEXT DEFAULT '',
  total_qty REAL NOT NULL DEFAULT 0,
  planned_qty REAL NOT NULL DEFAULT 0,
  finished_qty REAL NOT NULL DEFAULT 0,
  selected_bom_id INTEGER REFERENCES bom_variation(bom_id),
  planner_status TEXT NOT NULL DEFAULT 'UNPLANNED',
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  source_ps_id TEXT DEFAULT '',
  pp_partial_no TEXT DEFAULT '1'
);

CREATE TABLE IF NOT EXISTS process_sheet_material (
  mat_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ps_id TEXT NOT NULL REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
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

CREATE TABLE IF NOT EXISTS staff (
  staff_id INTEGER PRIMARY KEY AUTOINCREMENT,
  staff_name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'MACHINIST',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bom_material (
  bom_material_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_inventory_code TEXT NOT NULL DEFAULT '',
  bom_code TEXT NOT NULL DEFAULT '',
  material_inventory_code TEXT NOT NULL DEFAULT '',
  material_description TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source_inventory_code, bom_code, material_inventory_code)
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

CREATE TABLE IF NOT EXISTS material_requirement (
  requirement_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ps_id TEXT NOT NULL REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  source_inventory_code TEXT NOT NULL DEFAULT '',
  bom_code TEXT NOT NULL DEFAULT '',
  material_inventory_code TEXT NOT NULL DEFAULT '',
  material_description TEXT DEFAULT '',
  material_qty_needed REAL NOT NULL DEFAULT 0,
  material_uom TEXT DEFAULT '',
  supply_status TEXT NOT NULL DEFAULT 'PENDING_CONFIRMATION',
  expected_ready_date TEXT DEFAULT '',
  supplier_ref TEXT DEFAULT '',
  remarks TEXT DEFAULT '',
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(ps_id, material_inventory_code)
);

CREATE TABLE IF NOT EXISTS operation (
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

CREATE TABLE IF NOT EXISTS run_block_group (
  group_id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_label TEXT NOT NULL DEFAULT '',
  group_type TEXT NOT NULL DEFAULT 'COMBINED',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS run_block (
  block_id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_id INTEGER NOT NULL REFERENCES operation(operation_id) ON DELETE CASCADE,
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
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  block_type TEXT NOT NULL DEFAULT 'ORIGINAL',
  source_reject_block_id INTEGER,
  source_reject_segment_id INTEGER,
  planning_status TEXT NOT NULL DEFAULT 'UNPLANNED',
  execution_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
  anchor_status TEXT NOT NULL DEFAULT 'NONE',
  anchor_miss_minutes REAL NOT NULL DEFAULT 0,
  group_id INTEGER REFERENCES run_block_group(group_id)
);

CREATE TABLE IF NOT EXISTS run_block_segment (
  segment_id INTEGER PRIMARY KEY AUTOINCREMENT,
  block_id INTEGER NOT NULL REFERENCES run_block(block_id) ON DELETE CASCADE,
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

CREATE TABLE IF NOT EXISTS production_actual (
  actual_id INTEGER PRIMARY KEY AUTOINCREMENT,
  block_id INTEGER NOT NULL REFERENCES run_block(block_id) ON DELETE CASCADE,
  report_date TEXT NOT NULL,
  remarks TEXT DEFAULT '',
  reported_at TEXT DEFAULT CURRENT_TIMESTAMP,
  segment_id INTEGER REFERENCES run_block_segment(segment_id) ON DELETE SET NULL,
  output_qty REAL DEFAULT NULL,
  reject_qty REAL DEFAULT NULL,
  target_qty_at_report REAL DEFAULT NULL,
  UNIQUE(block_id, report_date)
);

CREATE TABLE IF NOT EXISTS public_holiday (
  holiday_date TEXT PRIMARY KEY,
  note TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS planning_card (
  card_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ps_id TEXT NOT NULL REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  operation_label TEXT NOT NULL DEFAULT '',
  target_qty REAL NOT NULL DEFAULT 0,
  planning_status TEXT NOT NULL DEFAULT 'UNSCHEDULED',
  card_type TEXT NOT NULL DEFAULT 'NORMAL',
  machine_id INTEGER REFERENCES machines(machine_id),
  scheduled_block_group_id INTEGER REFERENCES run_block_group(group_id),
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS planning_card_operation (
  card_op_id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id INTEGER NOT NULL REFERENCES planning_card(card_id) ON DELETE CASCADE,
  source_ps_id TEXT NOT NULL DEFAULT '',
  source_op_seq_id INTEGER NOT NULL DEFAULT 0,
  source_op_no TEXT NOT NULL DEFAULT '',
  op_sequence INTEGER NOT NULL DEFAULT 0,
  setup_minutes REAL NOT NULL DEFAULT 0,
  cycle_minutes_per_qty REAL NOT NULL DEFAULT 0,
  target_qty REAL NOT NULL DEFAULT 0
);

-- Optional read-only compatibility views for frontend/API aliases.
-- Do not add INSTEAD OF triggers here; app writes should use the real tables.

CREATE VIEW IF NOT EXISTS flows AS
SELECT
  bom_id AS bom_id,
  part_id,
  bom_code AS flow_code,
  bom_desc AS flow_name,
  is_default
FROM bom_variation;

CREATE VIEW IF NOT EXISTS flow_steps AS
SELECT
  op_seq_id AS op_seq_id,
  bom_id AS bom_id,
  seq_no AS seq,
  seq_no,
  op_no,
  op_type,
  machine_category,
  preferred_machine,
  cycle_time,
  setup_time,
  is_last_op
FROM operation_seq;

CREATE VIEW IF NOT EXISTS process_sheets AS
SELECT
  ps_id,
  part_id,
  selected_bom_id AS selected_bom_id,
  part_no AS part_no,
  part_desc AS part_desc,
  order_date,
  due_date,
  total_qty,
  status,
  planned_qty,
  finished_qty,
  planner_status,
  source_ps_id,
  pp_partial_no
FROM process_sheet;
