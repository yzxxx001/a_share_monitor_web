@echo off
cd /d %~dp0\..
call .venv\Scripts\activate.bat
stock-monitor once --config config\config.yaml
pause
