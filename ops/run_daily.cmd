@echo off
rem Daily multi-city sweep. ASCII only; uses %~dp0 so Chinese paths still work.
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if not exist "%~dp0logs" mkdir "%~dp0logs"
echo. >> "%~dp0logs\daily.log"
echo [%date% %time%] sweep start >> "%~dp0logs\daily.log"
"C:\Users\15647\anaconda3\python.exe" "%~dp0manage.py" sweep --due --limit 15 --channels auto --gap 15 --publish --push >> "%~dp0logs\daily.log" 2>&1
echo [%date% %time%] sweep end exit=%ERRORLEVEL% >> "%~dp0logs\daily.log"
endlocal
