@echo off
setlocal
pushd "%~dp0" || (
    echo Der Programmordner konnte nicht geoeffnet werden: "%~dp0"
    pause
    exit /b 1
)
set "PYMODE="

rem Python 3.11 oder neuer mit tkinter suchen: erst Python-Launcher (py -3), dann python
where py >nul 2>nul
if errorlevel 1 goto :trypython
py -3 -c "import sys,tkinter; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul
if errorlevel 1 goto :trypython
set "PYMODE=py"
goto :found

:trypython
where python >nul 2>nul
if errorlevel 1 goto :nopython
python -c "import sys,tkinter; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul
if errorlevel 1 goto :nopython
set "PYMODE=python"

:found
rem GUI ohne Konsolenfenster starten, dieses Fenster schliesst sich danach
if "%PYMODE%"=="py" goto :startpy
set "PYW="
for /f "delims=" %%i in ('python -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set "PYW=%%i"
if not defined PYW goto :startconsole
if not exist "%PYW%" goto :startconsole
start "" "%PYW%" -m xdroot_converter
exit /b 0

:startpy
where pyw >nul 2>nul
if errorlevel 1 goto :startconsole
start "" pyw -3 -m xdroot_converter
exit /b 0

:startconsole
if "%PYMODE%"=="py" (start "" py -3 -m xdroot_converter) else (start "" python -m xdroot_converter)
exit /b 0

:nopython
echo.
echo ============================================================
echo  XDROOT-Converter kann nicht gestartet werden.
echo.
echo  Es wurde kein passendes Python gefunden (benoetigt: Python 3.11
echo  oder neuer, mit tkinter).
echo.
echo  Bitte Python von https://www.python.org/downloads/ installieren
echo  und dabei im Installationsfenster beide Haken setzen:
echo    - "Add python.exe to PATH"
echo    - "tcl/tk and IDLE" (bei "Optional Features" bzw. "Customize")
echo  Danach diese Datei erneut doppelklicken.
echo ============================================================
echo.
pause
exit /b 1
