@echo off
cd /d %~dp0\..
call .venv\Scripts\activate.bat
stock-monitor loop --config config\config.yaml --run-now
pause
