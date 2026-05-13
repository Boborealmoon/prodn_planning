PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS parts (
  part_id INTEGER PRIMARY KEY AUTOINCREMENT,
  part_no TEXT NOT NULL UNIQUE,
  part_desc TEXT NOT NULL DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bom_variation (
  bom_id INTEGER PRIMARY KEY AUTOINCREMENT,
  part_id INTEGER NOT NULL REFERENCES parts(part_id) ON DELETE CASCADE,
  bom_code TEXT NOT NULL,
  bom_desc TEXT NOT NULL DEFAULT '',
  is_default INTEGER NOT NULL DEFAULT 0,
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
  preferred_machine TEXT NOT NULL DEFAULT '',
  is_last_op INTEGER NOT NULL DEFAULT 0,
  UNIQUE(bom_id, seq_no, op_no)
);

CREATE TABLE IF NOT EXISTS process_sheet (
  ps_id TEXT PRIMARY KEY,
  part_id INTEGER REFERENCES parts(part_id),
  part_no TEXT NOT NULL DEFAULT '',
  part_desc TEXT NOT NULL DEFAULT '',
  order_date TEXT NOT NULL DEFAULT '',
  due_date TEXT NOT NULL DEFAULT '',
  total_qty REAL NOT NULL DEFAULT 0,
  planned_qty REAL NOT NULL DEFAULT 0,
  finished_qty REAL NOT NULL DEFAULT 0,
  selected_bom_id INTEGER REFERENCES bom_variation(bom_id),
  planner_status TEXT NOT NULL DEFAULT 'UNPLANNED',
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  source_ps_id TEXT NOT NULL DEFAULT '',
  pp_partial_no TEXT NOT NULL DEFAULT '1'
);

CREATE TABLE IF NOT EXISTS machines (
  machine_id INTEGER PRIMARY KEY AUTOINCREMENT,
  machine_code TEXT NOT NULL,
  machine_category TEXT NOT NULL,
  shift_profile TEXT NOT NULL DEFAULT 'STANDARD',
  active INTEGER NOT NULL DEFAULT 1,
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS capacity_profile (
  profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_name TEXT NOT NULL,
  capacity_minutes INTEGER NOT NULL DEFAULT 0,
  start_minute INTEGER NOT NULL DEFAULT 510,
  note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS machine_capacity_day (
  day_id INTEGER PRIMARY KEY AUTOINCREMENT,
  machine_id INTEGER NOT NULL REFERENCES machines(machine_id) ON DELETE CASCADE,
  work_date TEXT NOT NULL,
  profile_id INTEGER NOT NULL REFERENCES capacity_profile(profile_id),
  capacity_minutes INTEGER NOT NULL DEFAULT 0,
  start_minute INTEGER NOT NULL DEFAULT 510,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS public_holiday (
  holiday_date TEXT PRIMARY KEY,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bom_material (
  bom_material_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_inventory_code TEXT NOT NULL DEFAULT '',
  bom_code TEXT NOT NULL DEFAULT '',
  material_inventory_code TEXT NOT NULL DEFAULT '',
  material_description TEXT NOT NULL DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source_inventory_code, bom_code, material_inventory_code)
);

CREATE TABLE IF NOT EXISTS material_requirement (
  requirement_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ps_id TEXT NOT NULL REFERENCES process_sheet(ps_id) ON DELETE CASCADE,
  source_inventory_code TEXT NOT NULL DEFAULT '',
  bom_code TEXT NOT NULL DEFAULT '',
  material_inventory_code TEXT NOT NULL DEFAULT '',
  material_description TEXT NOT NULL DEFAULT '',
  material_qty_needed REAL NOT NULL DEFAULT 0,
  material_uom TEXT NOT NULL DEFAULT '',
  supply_status TEXT NOT NULL DEFAULT 'PENDING_CONFIRMATION',
  expected_ready_date TEXT NOT NULL DEFAULT '',
  supplier_ref TEXT NOT NULL DEFAULT '',
  remarks TEXT NOT NULL DEFAULT '',
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
  compatible_machine_group TEXT NOT NULL DEFAULT '',
  source_ps_id TEXT NOT NULL DEFAULT '',
  source_op_seq_id INTEGER NOT NULL DEFAULT 0,
  source_op_no TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  remarks TEXT NOT NULL DEFAULT '',
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
  machine_id INTEGER NOT NULL REFERENCES machines(machine_id) ON DELETE CASCADE,
  queue_position INTEGER NOT NULL DEFAULT 0,
  scheduled_qty REAL NOT NULL DEFAULT 0,
  include_setup INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'PLANNED',
  anchor_datetime TEXT NOT NULL DEFAULT '',
  calculated_start_datetime TEXT NOT NULL DEFAULT '',
  calculated_end_datetime TEXT NOT NULL DEFAULT '',
  actual_good_qty REAL NOT NULL DEFAULT 0,
  actual_reject_qty REAL NOT NULL DEFAULT 0,
  remarks TEXT NOT NULL DEFAULT '',
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
  machine_id INTEGER NOT NULL REFERENCES machines(machine_id) ON DELETE CASCADE,
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
  remarks TEXT NOT NULL DEFAULT '',
  reported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  segment_id INTEGER,
  output_qty REAL,
  reject_qty REAL,
  target_qty_at_report REAL
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

CREATE TABLE IF NOT EXISTS data_import_log (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT,
  import_type TEXT NOT NULL,
  workbook_name TEXT NOT NULL DEFAULT '',
  active_sheet_name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'STARTED',
  message TEXT NOT NULL DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS staff (
  staff_id INTEGER PRIMARY KEY AUTOINCREMENT,
  staff_name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'MACHINIST',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS calendar_days (
  work_date TEXT PRIMARY KEY,
  is_working_day INTEGER NOT NULL DEFAULT 1,
  note TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_machines_machine_code_unique
ON machines(machine_code);

CREATE UNIQUE INDEX IF NOT EXISTS idx_capacity_profile_profile_name_unique
ON capacity_profile(profile_name);
