@echo off
rem Nightly long sweep: keeps going through cooldowns until the queue is done.
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if not exist "%~dp0logs" mkdir "%~dp0logs"
echo. >> "%~dp0logs\night.log"
echo [%date% %time%] night start >> "%~dp0logs\night.log"
"C:\Users\15647\anaconda3\python.exe" "%~dp0manage.py" night --due --limit 75 --minutes 240 --gap 15 --push >> "%~dp0logs\night.log" 2>&1
echo [%date% %time%] night end exit=%ERRORLEVEL% >> "%~dp0logs\night.log"
endlocal
