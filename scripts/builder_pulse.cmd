@echo off
where py >nul 2>nul && py -3 -c "import sys; raise SystemExit(sys.version_info ^< (3, 11))" >nul 2>nul && py -3 "%~dp0builder_pulse.py" hook && exit /b 0
where python >nul 2>nul && python -c "import sys; raise SystemExit(sys.version_info ^< (3, 11))" >nul 2>nul && python "%~dp0builder_pulse.py" hook && exit /b 0
where python3 >nul 2>nul && python3 -c "import sys; raise SystemExit(sys.version_info ^< (3, 11))" >nul 2>nul && python3 "%~dp0builder_pulse.py" hook && exit /b 0
echo Builder Pulse requires Python 3.11 or newer. 1>&2
exit /b 127
