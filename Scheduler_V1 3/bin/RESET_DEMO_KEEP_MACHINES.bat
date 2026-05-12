@echo off
echo ============================================================
echo  Local Production Planner - Reset Demo DB Keep Machines
echo ============================================================
echo.
echo WARNING: This will reset all demo/process/planning data
echo          but it will NOT delete the machines table data.
echo.
set /p CONFIRM="Type YES to confirm reset: "

if /i not "%CONFIRM%"=="YES" (
    echo Reset cancelled.
    pause
    exit /b 0
)

cd /d "%~dp0"

echo.
echo Resetting all tables except machines...
python -c "
import os, sqlite3
from datetime import date, timedelta

db_path = 'planner.db'
con = sqlite3.connect(db_path)
con.execute('PRAGMA foreign_keys = OFF')

tables_to_clear = [
    'planning_row',
    'planning_block',
    'history_row',
    'history_block',
    'process_sheet_material_order_log',
    'process_sheet_material',
    'process_sheet_manual_actual',
    'process_sheet_erp_link',
    'process_sheet_erp_actual',
    'machine_staff_assignment',
    'staff',
    'process_sheet',
    'part_flow_steps',
    'part_flow_header',
    'parts',
    'erp_process_sheets_staging',
    'erp_materials_per_ps_staging',
    'erp_materials_per_bom_staging',
    'erp_bom_op_stage_staging',
    'erp_workorder_tracker_staging',
    'erp_sync_log',
    'calendar_days',
]

for table in tables_to_clear:
    con.execute(f'DELETE FROM {table}')

con.execute(\"DELETE FROM sqlite_sequence WHERE name IN (%s)\" % ','.join('?' for _ in tables_to_clear), tables_to_clear)

seed_sql = open('seed_demo.sql', encoding='utf-8').read()
lines = []
skip = False
for line in seed_sql.splitlines():
    stripped = line.strip()
    if stripped.startswith('INSERT OR IGNORE INTO machines'):
        skip = True
        continue
    if skip:
        if stripped.endswith(';'):
            skip = False
        continue
    lines.append(line)
con.executescript('\\n'.join(lines))

today = date.today()
for i in range(-30, 90):
    d = today + timedelta(days=i)
    is_working = 0 if d.weekday() >= 5 else 1
    con.execute('INSERT OR IGNORE INTO calendar_days (work_date, is_working_day) VALUES (?,?)', (d.isoformat(), is_working))

con.execute('PRAGMA foreign_keys = ON')
con.commit()
con.close()
print('Reset complete. Machines preserved.')
"

echo.
echo ============================================================
echo  Demo database restored and machine data preserved.
echo  Run RUN_APP.bat to start the application.
echo ============================================================
pause
