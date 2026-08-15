@echo off
cd /d "C:\Users\34644\Downloads\Proyectos\wallapop-chollos-bot"
".venv\Scripts\python.exe" -m src.main --once > "logs\last_run_crash.log" 2>&1
if %ERRORLEVEL% EQU 0 del "logs\last_run_crash.log"
