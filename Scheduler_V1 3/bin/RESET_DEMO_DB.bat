@echo off
echo ============================================================
echo  Local Production Planner - Reset Demo Database
echo ============================================================
echo.
echo WARNING: This will delete ALL current data and restore demo data.
echo.
set /p CONFIRM="Type YES to confirm reset: "

if /i not "%CONFIRM%"=="YES" (
    echo Reset cancelled.
    pause
    exit /b 0
)

cd /d "%~dp0"

echo.
echo Deleting existing database...
if exist planner.db del planner.db

echo Restoring demo data...
python -c "
import sqlite3, os
db = sqlite3.connect('planner.db')
db.execute('PRAGMA foreign_keys = ON')
with open(r'..\scheduler_app\schema.sql', encoding='utf-8') as f:
    db.executescript(f.read())
with open('seed_demo.sql') as f:
    db.executescript(f.read())
# Generate calendar
from datetime import date, timedelta
today = date.today()
for i in range(-30, 90):
    d = today + timedelta(days=i)
    is_working = 0 if d.weekday() >= 5 else 1
    db.execute('INSERT OR IGNORE INTO calendar_days (work_date, is_working_day) VALUES (?,?)', (d.isoformat(), is_working))
db.commit()
db.close()
print('Database reset complete.')
"

echo.
echo ============================================================
echo  Demo database restored successfully.
echo  Run RUN_APP.bat to start the application.
echo ============================================================
pause
