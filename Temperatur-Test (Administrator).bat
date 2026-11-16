@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 goto :nodir
rem Startet die Temperatur-Diagnose in einer Administrator-Konsole (UAC-Abfrage); die Erhoehung macht Python.
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
echo Starte Temperatur-Test mit Administratorrechten (bitte die Windows-Abfrage bestaetigen) ...
if "%PYMODE%"=="py" (py -3 -m xdroot_converter.cli --thermal-test --als-administrator) else (python -m xdroot_converter.cli --thermal-test --als-administrator)
set "RC=%errorlevel%"
rem Exit-Codes: 0 = Test erhoeht gestartet bzw. erfolgreich; 1 = der Test lief hier und hat selbst auf Enter gewartet
rem (Python-Skript, Fenster war schon offen - kein zweites pause); 3 = Erhoehung abgelehnt/fehlgeschlagen (ausfuehrlicher
rem Hinweis); jeder andere Code (z. B. 2 = falsche Argumente, 9009 = Python fehlt) = Start fehlgeschlagen (kurzer Hinweis).
if "%RC%"=="0" exit /b 0
if "%RC%"=="3" goto :elevfail
if "%RC%"=="1" exit /b 1
goto :startfail

:startfail
echo.
echo Start fehlgeschlagen (Code %RC%).
pause
exit /b %RC%

:elevfail
echo.
echo Der Start mit Administratorrechten wurde abgebrochen oder ist fehlgeschlagen.
echo Der Temperatur-Test braucht Administratorrechte.
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
