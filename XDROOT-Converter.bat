@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw -m xdroot_converter
) else (
    start "" python -m xdroot_converter
)
