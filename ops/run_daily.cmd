@echo off
rem Daily ops runner for shanghai-events-demo.
rem Keep this file ASCII-only: cmd.exe reads it with the OEM codepage.
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if not exist "%~dp0logs" mkdir "%~dp0logs"
echo. >> "%~dp0logs\daily.log"
echo [%date% %time%] run start >> "%~dp0logs\daily.log"
"C:\Users\15647\anaconda3\python.exe" "%~dp0manage.py" run --publish --push --max-new 8 --enrich 20 >> "%~dp0logs\daily.log" 2>&1
echo [%date% %time%] run end exit=%ERRORLEVEL% >> "%~dp0logs\daily.log"
endlocal
