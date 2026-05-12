@echo off
echo ============================================================
echo  ERP Sync via 32-bit Python
echo ============================================================
echo.
echo Edit this file first and set PYTHON32 to your 32-bit Python path.
echo Example:
echo   set PYTHON32=C:\Python313-32\python.exe
echo.

set PYTHON32=C:\Users\vanneza\Downloads\Scheduler_V1\python_32bit\python.exe

if "%PYTHON32%"=="" (
  echo ERROR: PYTHON32 is not set.
  pause
  exit /b 1
)

cd /d "%~dp0"

"%PYTHON32%" erp_sync.py

pause
