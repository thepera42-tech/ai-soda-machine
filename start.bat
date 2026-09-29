@echo off
REM Windows launcher (for the laptop).
REM   start.bat            -> auto-detect the Arduino's COM port
REM   start.bat mock       -> simulated Arduino (no hardware)
REM   start.bat serial     -> same as auto, real Arduino
REM First time only:  pip install -r requirements.txt
setlocal
cd /d "%~dp0"
if not exist logs mkdir logs

set "MODE=%~1"
if "%MODE%"=="" set "MODE=auto"
set "PUMP_TRANSPORT=%MODE%"
if "%PUMP_PORT%"=="" set "PUMP_PORT=5005"

echo Starting pump backend (transport: %MODE%) ...
echo Opening http://localhost:%PUMP_PORT%/ in a few seconds. Close this window (or Ctrl+C) to stop.
start "" /b cmd /c "timeout /t 4 >nul & start http://localhost:%PUMP_PORT%/"

where python >nul 2>nul
if %errorlevel%==0 (
  python backend.py
) else (
  py backend.py
)
pause
