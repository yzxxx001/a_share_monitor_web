@echo off
cd /d %~dp0\..
call .venv\Scripts\activate.bat
stock-monitor validate --config config\config.yaml
pause
