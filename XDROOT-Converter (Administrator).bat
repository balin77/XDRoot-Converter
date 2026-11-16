@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 goto :nodir
rem Startet die GUI mit Administratorrechten (UAC-Abfrage); die Erhoehung macht Python (--als-administrator).
set "PYMODE="

rem Python 3.11 oder neuer mit tkinter suchen: erst Python-Launcher (py -3), dann python (wie XDROOT-Converter.bat)
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
echo Starte XDROOT-Converter mit Administratorrechten (bitte die Windows-Abfrage bestaetigen) ...
if "%PYMODE%"=="py" (py -3 -m xdroot_converter --als-administrator) else (python -m xdroot_converter --als-administrator)
set "RC=%errorlevel%"
rem Exit-Code 3 = die Erhoehung wurde abgelehnt oder ist fehlgeschlagen; alles andere ist das Ergebnis des Programms selbst
if "%RC%"=="3" goto :elevfail
exit /b %RC%

:elevfail
echo.
echo Der Start mit Administratorrechten wurde abgebrochen oder ist fehlgeschlagen.
echo Ohne Administratorrechte funktioniert die Umwandlung weiterhin - nur ohne Temperaturanzeige
echo und Luefterschaltung. Dafuer die normale Startdatei XDROOT-Converter.bat benutzen.
pause
exit /b 1

:nodir
echo Der Programmordner konnte nicht geoeffnet werden: "%~dp0"
pause
exit /b 1

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
