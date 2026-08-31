@echo off
py -3 "%~dp0builder_pulse.py" hook
exit /b %errorlevel%
