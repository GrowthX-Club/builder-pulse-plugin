@echo off
set "BUILDER_PULSE_AGENT_PLATFORM=claude_code"
set "BUILDER_PULSE_PLUGIN_VERSION=0.5.2"
if defined BUILDER_PULSE_RUNTIME_DIR (set "BP_RUNTIME=%BUILDER_PULSE_RUNTIME_DIR%") else (set "BP_RUNTIME=%USERPROFILE%\.builder-pulse\runtime\0.5.2")
if not exist "%BP_RUNTIME%\scripts\builder_pulse.py" goto success
where py >nul 2>nul && py -3 -c "import sys; raise SystemExit(not (sys.version_info.major in range(4, 100) or (sys.version_info.major == 3 and sys.version_info.minor in range(11, 100))))" >nul 2>nul && py -3 "%BP_RUNTIME%\scripts\builder_pulse.py" hook >nul 2>nul && goto success
where python >nul 2>nul && python -c "import sys; raise SystemExit(not (sys.version_info.major in range(4, 100) or (sys.version_info.major == 3 and sys.version_info.minor in range(11, 100))))" >nul 2>nul && python "%BP_RUNTIME%\scripts\builder_pulse.py" hook >nul 2>nul && goto success
where python3 >nul 2>nul && python3 -c "import sys; raise SystemExit(not (sys.version_info.major in range(4, 100) or (sys.version_info.major == 3 and sys.version_info.minor in range(11, 100))))" >nul 2>nul && python3 "%BP_RUNTIME%\scripts\builder_pulse.py" hook >nul 2>nul && goto success
:success
echo {}
exit /b 0
