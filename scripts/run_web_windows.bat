@echo off
cd /d %~dp0\..
call .venv\Scripts\activate
stock-monitor web --config config\config.yaml
