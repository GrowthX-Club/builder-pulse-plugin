@echo off
set "BUILDER_PULSE_AGENT_PLATFORM=codex"
set "BUILDER_PULSE_PLUGIN_VERSION=0.5.0"
where py >nul 2>nul && py -3 -c "import sys; raise SystemExit(not (sys.version_info.major in range(4, 100) or (sys.version_info.major == 3 and sys.version_info.minor in range(11, 100))))" >nul 2>nul && py -3 "%~dp0builder_pulse.py" hook >nul 2>nul && goto success
where python >nul 2>nul && python -c "import sys; raise SystemExit(not (sys.version_info.major in range(4, 100) or (sys.version_info.major == 3 and sys.version_info.minor in range(11, 100))))" >nul 2>nul && python "%~dp0builder_pulse.py" hook >nul 2>nul && goto success
where python3 >nul 2>nul && python3 -c "import sys; raise SystemExit(not (sys.version_info.major in range(4, 100) or (sys.version_info.major == 3 and sys.version_info.minor in range(11, 100))))" >nul 2>nul && python3 "%~dp0builder_pulse.py" hook >nul 2>nul && goto success
:success
echo {}
exit /b 0
