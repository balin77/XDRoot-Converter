"""Temperaturüberwachung und Lüfterschaltung (Schonmodus, Runde 3).

Aufbau (siehe docs/SPEC.md, Abschnitt "Temperaturüberwachung und Schonmodus"):

* **Anbieter** (``ThermalProvider``, hardwareabhängig): liefert eine ``Messung`` und kann "Lüfter voll"
  ein- und ausschalten.  Mitgeliefert wird ``LegionProvider`` (Lenovo Legion, WMI ``root\\wmi``).  Der WMI-Zugriff
  braucht Administratorrechte und läuft über EINEN dauerhaft laufenden, versteckten PowerShell-Hilfsprozess
  (``PowerShellHelper``): Befehle zeilenweise über stdin, Antworten zeilenweise über stdout, je Antwort ein Zeitlimit.
* **Regler** (``ThermalController``, hardwareunabhängig): Schwellen mit Hysterese, mehrere Messungen in Folge
  entscheiden.  Pausiert über einen ``pause_ctl`` (``tools.CancelToken``: ``pause()``/``resume()``).
* **Sitzung** (``ThermalSession``): Verfügbarkeitsprüfung + eigener Messthread; wird vom Ablauf benutzt.

Ein Fehler der Überwachung ist NIE ein Fehler der Umwandlung: Der Anbieter gilt dann als ausgefallen, ein
angehaltener Prozess wird fortgesetzt, der Lüfterzustand wiederhergestellt, die Umwandlung läuft unverändert weiter.

Einen Anbieter für anderes Gerät ergänzen (Beispiel)::

    class MeinAnbieter(ThermalProvider):
        name = "Mein Gerät"
        def open(self):                       # ProviderUnavailable(grund, code) werfen, wenn nicht nutzbar
            self._h = meine_schnittstelle()
        def measure(self):                    # Messung(cpu_temp_c, fan_rpm=(1200, 1300)); bei Fehler ProviderError
            return Messung(self._h.cpu_grad(), (self._h.fan1(),))
        def fan_control_info(self):           # (kann_schalten, Beschreibung); gültig nach open()
            return True, "Lüfter über ..."
        def fan_full(self): ...               # Lüfter voll (merkt/liest den vorherigen Zustand VOR dem Schalten)
        def fan_restore(self): ...            # vorherigen Zustand wiederherstellen (mehrfach aufrufbar)
        def close(self): ...

    ThermalSession(..., provider_factory=MeinAnbieter)
"""

from __future__ import annotations

import atexit
import contextlib
import itertools
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import tools

# --------------------------------------------------------------------------- Konstanten
TEMP_MAX_PLAUSIBLE_C = 125.0  # Messung <= 0 oder > 125 Grad gilt als unplausibel
DEFAULT_FAN_ON_C = 85.0
DEFAULT_PAUSE_C = 97.0
FAN_HYSTERESIS_C = 10.0  # Lüfter zurück unter (Lüfter-voll-Schwelle - 10), Standard 85 -> 75
PAUSE_HYSTERESIS_C = 12.0  # weiter unter (Pausen-Schwelle - 12), Standard 97 -> 85
DEFAULT_INTERVAL_S = 5.0
HELPER_STARTUP_TIMEOUT_S = 30.0  # PowerShell-Start + erste WMI-Abfrage
HELPER_TIMEOUT_S = 10.0  # Zeitlimit je Messung; danach wird der Hilfsprozess beendet (und einmal neu gestartet)
HELPER_WRITE_TIMEOUT_S = 30.0  # Zeitlimit für Schalt- und Rückleseaufrufe (WMI-Schreiben kann langsam sein)
DEFAULT_MAX_PAUSE_S = 20 * 60.0  # längste Temperaturpause; danach wird der Lauf sauber abgebrochen
PAUSE_LOG_INTERVAL_S = 120.0  # während einer Pause alle 2 Minuten eine Protokollzeile
# Grenzen der einstellbaren Schwellen (GUI und CLI gleich)
TEMP_FAN_MIN, TEMP_FAN_MAX = 50.0, 110.0
TEMP_PAUSE_MIN, TEMP_PAUSE_MAX = 60.0, 120.0
# Wirkungsprüfung "Lüfter voll": Die Drehzahl muss innerhalb von FAN_EFFECT_WINDOW_S gegenüber dem Wert VOR dem
# Schalten um mindestens FAN_EFFECT_RISE (+15 %) gestiegen sein ODER mindestens FAN_EFFECT_NEAR_MAX (90 %) der
# gemeldeten Höchstdrehzahl (GetFanMaxSpeed) erreichen.  Beides ist eine Festlegung, keine Messvorschrift des Herstellers.
FAN_EFFECT_WINDOW_S = 20.0
FAN_EFFECT_RISE = 1.15
FAN_EFFECT_NEAR_MAX = 0.90
FAN_MAX_PLAUSIBLE_MIN, FAN_MAX_PLAUSIBLE_MAX = 1000.0, 12000.0  # GetFanMaxSpeed nur in diesem Bereich (U/min) benutzen
MAX_HELPER_RESTARTS = 3  # Neustarts des Hilfsprozesses je Lauf wegen Messfehlern, danach gilt der Anbieter als ausgefallen
RESTORE_MAX_ATTEMPTS = 3  # gescheiterte Wiederherstellungen beim Start, danach wird die Zustandsdatei verworfen

# --- Lenovo Legion (WMI root\wmi).  Die Klassen/Methoden sind am Gerät gemessen (Get-CimClass).
# Welche KENNUNG "Lüfter voll" auslöst, ist NICHT belegt (Vermutung aus dem Open-Source-Projekt "Lenovo Legion
# Toolkit", Legion-Generationen unterscheiden sich) - am Gerät mit --thermal-test zu bestätigen.
WMI_NAMESPACE = "root\\wmi"
WMI_CLASS_GAMEZONE = "LENOVO_GAMEZONE_DATA"
WMI_CLASS_OTHER = "LENOVO_OTHER_METHOD"
# Neuere Legion-Generationen: LENOVO_OTHER_METHOD.SetFeatureValue(IDs=0x04020000, value=1 an / 0 aus).
# (am Gerät mit --thermal-test zu bestätigen)
FEATURE_ID_FAN_FULL_SPEED = 0x04020000
FEATURE_VALUE_ON = 1
FEATURE_VALUE_OFF = 0
# SmartFanMode (LENOVO_GAMEZONE_DATA.GetSmartFanMode) wird NUR GELESEN und in --thermal-test angezeigt.  Er wird NIE
# geschaltet: Der Leistungsmodus (3) hebt die CPU-Leistungsgrenzen an und erzeugt MEHR Wärme.
STATE_FILE_NAME = "fan_state.json"

_LIVE_HELPERS: "weakref.WeakSet" = weakref.WeakSet()
_LIVE_CONTROLLERS: "weakref.WeakSet" = weakref.WeakSet()


# --------------------------------------------------------------------------- Fehler
class ThermalError(Exception):
    """Basis aller Fehler der Temperaturüberwachung."""


class ProviderError(ThermalError):
    """Ein Anbieter-Aufruf ist fehlgeschlagen (Messung/Schalten)."""


class ProviderUnavailable(ThermalError):
    """Anbieter nicht nutzbar.  ``reason`` = deutscher Grund; ``code``: admin | nodevice | powershell | platform |
    implausible | other."""

    def __init__(self, reason: str, code: str = "other") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


class HelperError(ThermalError):
    """Hilfsprozess nicht startbar, beendet oder nicht lesbar."""


class HelperTimeout(HelperError):
    """Keine Antwort innerhalb des Zeitlimits (der Hilfsprozess wurde beendet)."""


class HelperBusy(HelperError):
    """Der Hilfsprozess wird gerade von einem anderen Aufruf benutzt (Sperre nicht rechtzeitig frei).  Er wurde NICHT
    beendet: Der Halter der Sperre hat sein eigenes Zeitlimit."""


class FanControlBusy(ProviderError):
    """Die Lüftersteuerung wird von einem anderen Lauf benutzt (es wird nicht geschaltet)."""


class HelperRemoteError(ThermalError):
    """Der Hilfsprozess meldet einen Fehler des Befehls (z. B. WMI "Zugriff verweigert")."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}" if code else message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- Messung / Konfiguration
@dataclass
class Messung:
    cpu_temp_c: float
    fan_rpm: tuple = ()  # Drehzahlen der Lüfter (U/min), leer = unbekannt
    fan_max_rpm: Optional[int] = None


def plausible(temp_c) -> bool:
    try:
        t = float(temp_c)
    except (TypeError, ValueError):
        return False
    return t == t and 0.0 < t <= TEMP_MAX_PLAUSIBLE_C


@dataclass
class ThermalConfig:
    fan_on_c: float = DEFAULT_FAN_ON_C
    fan_off_c: float = DEFAULT_FAN_ON_C - FAN_HYSTERESIS_C
    pause_c: float = DEFAULT_PAUSE_C
    resume_c: float = DEFAULT_PAUSE_C - PAUSE_HYSTERESIS_C
    confirm_up: int = 2  # Messungen in Folge über der Schwelle, bevor Lüfter voll/Pause
    confirm_down: int = 3  # Messungen in Folge unter der Rückschwelle, bevor zurück/weiter
    bad_limit: int = 3  # unplausible Messungen in Folge -> Anbieter gilt als ausgefallen
    max_pause_s: float = DEFAULT_MAX_PAUSE_S  # längste Pause; danach Abbruch des Laufs (fortsetzbar)
    pause_log_s: float = PAUSE_LOG_INTERVAL_S  # Protokollzeile während einer Pause
    fan_effect_window_s: float = FAN_EFFECT_WINDOW_S

    @classmethod
    def from_limits(cls, fan_on_c: float = DEFAULT_FAN_ON_C, pause_c: float = DEFAULT_PAUSE_C, **kw) -> "ThermalConfig":
        """Rückschwellen aus den beiden einstellbaren Schwellen ableiten (Standard 85/75 und 97/85)."""
        cfg = cls(fan_on_c=float(fan_on_c), fan_off_c=float(fan_on_c) - FAN_HYSTERESIS_C,
                  pause_c=float(pause_c), resume_c=float(pause_c) - PAUSE_HYSTERESIS_C, **kw)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        for n in ("fan_on_c", "fan_off_c", "pause_c", "resume_c"):
            v = getattr(self, n)
            if not plausible(v):
                raise ValueError(f"Temperaturschwelle {v} °C ist unplausibel (erlaubt: über 0 bis {TEMP_MAX_PLAUSIBLE_C:.0f}).")
        if not self.fan_off_c < self.fan_on_c:
            raise ValueError("Die Rückschwelle der Lüfter muss unter der Einschaltschwelle liegen.")
        if not self.resume_c < self.pause_c:
            raise ValueError("Die Rückschwelle der Pause muss unter der Pausenschwelle liegen.")
        if not self.fan_on_c < self.pause_c:
            raise ValueError("Die Pausenschwelle muss über der Lüfter-voll-Schwelle liegen.")
        if self.confirm_up < 1 or self.confirm_down < 1 or self.bad_limit < 1:
            raise ValueError("Anzahl der bestätigenden Messungen muss mindestens 1 sein.")
        if not self.max_pause_s > 0:
            raise ValueError("Die Höchstdauer einer Pause muss positiv sein.")


def check_limits(fan_c: float, pause_c: float, fan_name: str = "Lüfter voll ab", pause_name: str = "Pause ab") -> None:
    """Gemeinsame Prüfung der beiden einstellbaren Schwellen (GUI und CLI).  ValueError mit deutscher Meldung, die den
    EINGEGEBENEN Wert nennt."""
    for name, v, lo, hi in ((fan_name, fan_c, TEMP_FAN_MIN, TEMP_FAN_MAX), (pause_name, pause_c, TEMP_PAUSE_MIN, TEMP_PAUSE_MAX)):
        if v != v or not (lo <= v <= hi):
            raise ValueError(f"{name}: {v:g} °C liegt außerhalb des erlaubten Bereichs ({lo:.0f} bis {hi:.0f} °C).")
    if fan_c >= pause_c:
        raise ValueError(f"{pause_name} ({pause_c:g} °C) muss höher sein als {fan_name} ({fan_c:g} °C).")


def plausible_fan_max(fan_max, rpm_before: Optional[float] = None) -> Optional[float]:
    """GetFanMaxSpeed nur verwenden, wenn plausibel: 1000 bis 12000 U/min und nicht unter der Drehzahl vorher
    (andere Einheit, z. B. 100, oder Unsinn -> None)."""
    try:
        v = float(fan_max)
    except (TypeError, ValueError):
        return None
    if not (FAN_MAX_PLAUSIBLE_MIN <= v <= FAN_MAX_PLAUSIBLE_MAX):
        return None
    if rpm_before and rpm_before > v:
        return None
    return v


def fans_near_max(rpm_before: Optional[float], fan_max) -> bool:
    """Liefen die Lüfter schon vorher bei mindestens 90 % des (plausiblen) Maximums?"""
    mx = plausible_fan_max(fan_max, rpm_before)
    return bool(mx and rpm_before and rpm_before >= mx * FAN_EFFECT_NEAR_MAX)


def fan_effect_confirmed(rpm_before: Optional[float], rpm_now: Optional[float], fan_max: Optional[float] = None) -> bool:
    """Hat "Lüfter voll" gewirkt?  Ja, wenn die Drehzahl gegenüber vorher um mindestens 15 % gestiegen ist.  Die
    90-%-Regel (mindestens 90 % der gemeldeten Höchstdrehzahl) gilt nur bei plausiblem Maximum (1000-12000 U/min,
    nicht unter der Drehzahl vorher) UND wenn die Drehzahl vorher unter 90 % des Maximums lag (siehe FAN_EFFECT_*)."""
    if not rpm_now or rpm_now <= 0:
        return False
    if rpm_before and rpm_before > 0 and rpm_now >= rpm_before * FAN_EFFECT_RISE:
        return True
    mx = plausible_fan_max(fan_max, rpm_before)
    if mx and rpm_before and rpm_before > 0 and rpm_before < mx * FAN_EFFECT_NEAR_MAX:
        return rpm_now >= mx * FAN_EFFECT_NEAR_MAX
    return False


def fmt_span(seconds: float) -> str:
    """Dauer für Meldungen: unter 2 Minuten in Sekunden ('90 s'), sonst in Minuten ('20 min')."""
    seconds = max(0.0, float(seconds))
    return f"{seconds:.0f} s" if seconds < 120 else f"{seconds / 60:.0f} min"


def format_status(m: Messung, fan_full: bool = False, paused: bool = False, cfg: Optional[ThermalConfig] = None,
                  fan_effect: str = "confirmed", fan_reason: str = "") -> str:
    """Statuszeile der Oberfläche, z. B. 'CPU 87 °C · Lüfter 4800/5100 U/min · Lüfter voll'.

    ``fan_effect`` (nur bei ``fan_full``): confirmed = Drehzahl gestiegen -> 'Lüfter voll'; pending/unknown = 'Lüfter voll
    angefordert' (Wirkung noch nicht geprüft bzw. keine Drehzahl lesbar); none = '... (keine Wirkung messbar)'.
    ``fan_reason``: Grund, warum die Lüftersteuerung nicht verfügbar ist (wird angehängt)."""
    cfg = cfg or ThermalConfig()
    t = m.cpu_temp_c
    if paused:
        return f"Pause wegen Temperatur ({t:.0f} °C), weiter unter {cfg.resume_c:.0f} °C"
    parts = [f"CPU {t:.0f} °C"]
    if m.fan_rpm:
        parts.append("Lüfter " + "/".join(str(int(r)) for r in m.fan_rpm) + " U/min")
    if fan_full:
        if fan_effect == "confirmed":
            parts.append("Lüfter voll")
        elif fan_effect == "nearmax":
            parts.append("Lüfter laufen bereits nahe Maximum")
        elif fan_effect == "none":
            parts.append("Lüfter voll angefordert (keine Wirkung messbar)")
        else:
            parts.append("Lüfter voll angefordert")
    elif fan_reason:
        parts.append(fan_reason if fan_reason.startswith("Lüftersteuerung") else f"Lüftersteuerung nicht verfügbar ({fan_reason})")
    return " · ".join(parts)


def is_admin() -> bool:
    """Läuft dieses Programm mit Administratorrechten (Windows) bzw. als root (sonst)?"""
    try:
        if sys.platform == "win32":
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- Anbieter-Schnittstelle
class ThermalProvider:
    """Schnittstelle eines Temperatur-/Lüfteranbieters.  Alle Methoden dürfen ``ThermalError`` werfen."""

    name = "?"

    def open(self) -> None:
        """Anbieter starten/prüfen.  Wirft ``ProviderUnavailable`` (mit deutschem Grund), wenn nicht nutzbar.
        Liest hier auch den Lüfterzustand (nur lesend)."""
        raise NotImplementedError

    def measure(self) -> Messung:
        raise NotImplementedError

    def fan_control_info(self) -> tuple:
        """(kann_schalten, Beschreibung bzw. Grund).  Gültig nach ``open()``."""
        return False, "keine Lüfterschaltung"

    def fan_full(self) -> None:
        """Lüfter voll schalten (Zustand vorher war in ``open()`` gelesen).  Wirft bei Misserfolg."""
        raise ProviderError("Lüfterschaltung nicht unterstützt")

    def fan_restore(self) -> None:
        """Vorherigen Lüfterzustand wiederherstellen (mehrfach aufrufbar)."""
        raise ProviderError("Lüfterschaltung nicht unterstützt")

    def take_messages(self) -> list:
        """Hinweise des Anbieters seit dem letzten Aufruf als [(Stufe, Text)] für das Protokoll (z. B. 'Lüfterzustand
        nach abgebrochenem Lauf wiederhergestellt')."""
        return []

    def abort(self) -> None:
        """Sofort aufgeben (aus einem anderen Thread, z. B. während ``open()`` noch läuft).  Standard: ``close()``."""
        self.close()

    def begin_close(self) -> None:
        """Der Lauf endet (``finish()``/``stop()``): ab jetzt keine neuen Messungen/Neustarts mehr, aber das Zurückstellen
        muss noch gehen.  Standard: nichts."""

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- PowerShell-Hilfsprozess
# Festes Skript (kein frei wählbarer Code): nur "PING", "EXIT" und "CALL <LENOVO_*-Klasse> <Methode> Name=Zahl ...".
# Zeile rein: "<id> <befehl>", Zeile raus: "<id> OK <Name=Wert ...>" oder "<id> ERR <Code>|<Meldung>".
_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Import-Module "$PSHOME\Modules\CimCmdlets"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$script:inst = @{}
function Get-Inst([string]$cls) {
  if (-not $script:inst.ContainsKey($cls)) {
    $script:inst[$cls] = Get-CimInstance -Namespace 'root\wmi' -ClassName $cls | Select-Object -First 1
  }
  if ($null -eq $script:inst[$cls]) { throw ('InvalidClass: keine Instanz von ' + $cls) }
  return $script:inst[$cls]
}
function Handle([string]$cmd) {
  $t = $cmd.Split(' ')
  if ($t[0] -eq 'PING') { return 'pong' }
  if ($t[0] -ne 'CALL' -or $t.Count -lt 3) { throw 'BadCommand' }
  $cls = $t[1]; $m = $t[2]
  if ($cls -notmatch '^LENOVO_[A-Z_]+$' -or $m -notmatch '^[A-Za-z0-9_]+$') { throw 'BadArgs' }
  $prm = @{}
  for ($i = 3; $i -lt $t.Count; $i++) {
    if ($t[$i] -notmatch '^([A-Za-z]+)=([0-9]{1,10})$') { throw 'BadArgs' }
    $prm[$Matches[1]] = [uint32]$Matches[2]
  }
  $r = Invoke-CimMethod -InputObject (Get-Inst $cls) -MethodName $m -Arguments $prm
  $o = @()
  foreach ($n in 'Data', 'IDs', 'value', 'ReturnValue') {
    $p = $r.PSObject.Properties[$n]
    if ($p -and $null -ne $p.Value) { $o += ('{0}={1}' -f $n, $p.Value) }
  }
  return ($o -join ' ')
}
while ($true) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line) { break }
  $line = $line.Trim()
  if ($line -eq '') { continue }
  $sp = $line.IndexOf(' ')
  if ($sp -lt 0) { $id = $line; $cmd = '' } else { $id = $line.Substring(0, $sp); $cmd = $line.Substring($sp + 1) }
  if ($cmd -eq 'EXIT') { break }
  try {
    $res = Handle $cmd
    [Console]::Out.WriteLine("$id OK $res")
  } catch {
    $code = ''
    try { $code = [string]$_.Exception.NativeErrorCode } catch { }
    $msg = ($_.Exception.Message -replace '[\r\n|]+', ' ')
    [Console]::Out.WriteLine("$id ERR $code|$msg")
  }
  [Console]::Out.Flush()
}
"""


def _system_directory() -> Optional[str]:
    """Windows-Systemverzeichnis über GetSystemDirectoryW (nicht über PATH oder Umgebungsvariablen); None = unbekannt."""
    return tools.system_directory()


def system_powershell_path() -> Optional[str]:
    """Fester Pfad der Windows-PowerShell: <Systemverzeichnis>\WindowsPowerShell\v1.0\powershell.exe.  Es wird bewusst
    NICHT per PATH gesucht (shutil.which findet unter Python 3.11 zuerst Dateien im Arbeitsordner - fremder Code mit
    Administratorrechten) und kein pwsh benutzt.  None, wenn die Datei fehlt."""
    sysdir = _system_directory()
    if not sysdir:
        return None
    exe = os.path.join(sysdir, "WindowsPowerShell", "v1.0", "powershell.exe")
    return exe if os.path.isfile(exe) else None


def powershell_argv() -> list:
    """Kommandozeile des versteckten PowerShell-Hilfsprozesses.  Wirft HelperError, wenn PowerShell fehlt."""
    import base64

    exe = system_powershell_path()
    if exe is None:
        raise HelperError("Windows-PowerShell nicht gefunden")
    enc = base64.b64encode(_PS_SCRIPT.encode("utf-16-le")).decode("ascii")
    return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc]


class PowerShellHelper:
    """Ein dauerhaft laufender Hilfsprozess (Zeilenprotokoll, siehe ``_PS_SCRIPT``).

    ``argv`` und ``popen`` sind austauschbar (Tests).  Jede Antwort hat ein Zeitlimit; bei Zeitüberschreitung wird
    der Prozess beendet und der Helfer gilt als ausgefallen (``HelperTimeout``, danach nur noch ``HelperError``).
    Ist die Sperre für einen Aufruf belegt, wartet er höchstens sein eigenes Zeitlimit und wirft dann ``HelperBusy``
    - der Hilfsprozess wird dabei NICHT beendet (der Halter der Sperre hat sein eigenes Zeitlimit)."""

    def __init__(self, argv: Optional[list] = None, startup_timeout: float = HELPER_STARTUP_TIMEOUT_S,
                 timeout: float = HELPER_TIMEOUT_S, popen: Callable = subprocess.Popen) -> None:
        self._argv = argv
        self.startup_timeout = startup_timeout
        self.timeout = timeout
        self._popen = popen
        self._proc = None
        self._q: "queue.Queue" = queue.Queue()
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._dead = False

    @property
    def alive(self) -> bool:
        return self._proc is not None and not self._dead

    def start(self) -> None:
        argv = self._argv if self._argv is not None else powershell_argv()
        try:
            self._proc = self._popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=tools.CREATE_NO_WINDOW,  # kein Konsolenfenster
            )
        except (OSError, ValueError) as e:
            self._dead = True
            raise HelperError(f"Hilfsprozess nicht startbar: {e}") from e
        try:
            tools._assign_to_job(self._proc)  # Windows: bei hartem Kill dieses Programms mit beenden
        except Exception:  # noqa: BLE001
            pass
        if self._dead:  # währenddessen abgebrochen (abort())
            self._kill()
            raise HelperError("Hilfsprozess abgebrochen")
        _LIVE_HELPERS.add(self)
        threading.Thread(target=self._read_loop, args=(self._proc, self._q), daemon=True).start()
        try:
            reply = self._request("PING", self.startup_timeout)
        except BaseException:
            self.close(grace=0.0)
            raise
        if reply.strip() != "pong":
            self.close(grace=0.0)
            raise HelperError(f"unerwartete Antwort des Hilfsprozesses: {reply[:60]!r}")

    @staticmethod
    def _read_loop(proc, q: "queue.Queue") -> None:
        try:
            while True:
                raw = proc.stdout.readline()
                if not raw:
                    break
                q.put(raw.decode("utf-8", errors="replace").lstrip("﻿").rstrip("\r\n"))
        except Exception:  # noqa: BLE001
            pass
        finally:
            q.put(None)

    def call(self, command: str, timeout: Optional[float] = None, lock_timeout: Optional[float] = None) -> str:
        """Befehl senden, Nutzdaten der Antwort liefern.  Wirft HelperRemoteError (Befehl fehlgeschlagen),
        HelperTimeout (keine Antwort; Prozess beendet), HelperBusy (Sperre belegt; Prozess NICHT beendet) oder HelperError."""
        t = self.timeout if timeout is None else timeout
        if not self._lock.acquire(timeout=lock_timeout if lock_timeout is not None else t):
            # NICHT beenden: der Halter der Sperre (z. B. ein anderer Thread beim Zurückstellen) hat sein eigenes Zeitlimit
            raise HelperBusy("Hilfsprozess ist belegt (ein anderer Aufruf läuft noch)")
        try:
            return self._request(command, t)
        finally:
            self._lock.release()

    def _request(self, command: str, timeout: float) -> str:
        if not self.alive:
            raise HelperError("Hilfsprozess nicht aktiv")
        rid = str(next(self._ids))
        try:
            self._proc.stdin.write((rid + " " + command + "\n").encode("ascii", errors="replace"))
            self._proc.stdin.flush()
        except (OSError, ValueError) as e:
            self._kill()
            raise HelperError(f"Hilfsprozess nicht beschreibbar: {e}") from e
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise queue.Empty
                item = self._q.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                if remaining > 0 and self._dead:  # von aussen abgebrochen (abort())
                    self._kill()
                    raise HelperError("Hilfsprozess abgebrochen") from None
                if remaining > 0:
                    continue
                self._kill()
                raise HelperTimeout(f"keine Antwort des Hilfsprozesses innerhalb von {timeout:g} s") from None
            if item is None:
                self._dead = True
                self._q.put(None)
                raise HelperError("Hilfsprozess wurde beendet")
            parts = item.split(" ", 2)
            if len(parts) < 2 or parts[0] != rid:
                continue  # fremde/alte Zeile
            payload = parts[2] if len(parts) > 2 else ""
            if parts[1] == "OK":
                return payload
            if parts[1] == "ERR":
                code, _, msg = payload.partition("|")
                raise HelperRemoteError(code.strip(), msg.strip())

    def abort(self) -> None:
        """Hilfsprozess sofort hart beenden (aus einem anderen Thread)."""
        self._kill()

    def _kill(self) -> None:
        self._dead = True
        proc = self._proc
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass

    def close(self, grace: float = 2.0) -> None:
        """Hilfsprozess sicher beenden (erst 'EXIT', dann hart); mehrfach aufrufbar, wirft nie."""
        proc = self._proc
        self._dead = True
        if proc is None:
            return
        try:
            try:
                proc.stdin.write(b"0 EXIT\n")
                proc.stdin.flush()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            if grace > 0:
                try:
                    proc.wait(timeout=grace)
                except Exception:  # noqa: BLE001
                    pass
            try:
                if proc.poll() is None:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                proc.stdout.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- Zustandsdatei (Lüfter)
def default_state_dir() -> Optional[Path]:
    """Ordner der Zustandsdatei: Windows %LOCALAPPDATA%\\XDROOT-Converter, sonst ~/.cache/xdroot-converter.  None = unbekannt."""
    try:
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA")
            return Path(base) / "XDROOT-Converter" if base else None
        return Path.home() / ".cache" / "xdroot-converter"
    except Exception:  # noqa: BLE001
        return None


# Kennungen der Anbieter-Instanzen DIESES Prozesses, die gerade eine Zustandsdatei halten (zwei Anbieter im selben
# Prozess, z. B. in Tests, erkennen so den Besitzer, obwohl die PID gleich ist)
_HELD_TOKENS: set = set()


class FanStateStore:
    """Kleine JSON-Datei mit dem Lüfterzustand VOR dem ersten Schalten (+ PID, Prozess-Startzeit und Kennung des Besitzers).

    Sie wird unmittelbar vor dem ersten Schalten EXKLUSIV angelegt (``claim``: O_CREAT|O_EXCL) und nach erfolgreichem
    Zurückstellen vom Besitzer gelöscht (``release``: nur die EIGENE Datei).  So können (1) zwei Läufe den Zustand nicht
    verdrehen (existiert die Datei und der Besitzer lebt, schaltet der zweite Lauf nicht) und (2) ein Lauf nach
    Absturz/Stromausfall/hartem Beenden den Zustand wiederherstellen (Besitzer tot -> gespeicherten Zustand zurückschreiben).

    ``read`` akzeptiert nur Werte, die der Anbieter selbst schreiben würde (Kennung = Lüfter voll, Zustand 0 oder 1, ...);
    alles andere gilt als beschädigt (die Datei kann von jedem Prozess desselben Benutzers verändert werden)."""

    def __init__(self, directory=None) -> None:
        self.dir = Path(directory) if directory is not None else default_state_dir()

    @property
    def path(self) -> Optional[Path]:
        return self.dir / STATE_FILE_NAME if self.dir is not None else None

    def check_writable(self) -> str:
        """'' wenn die Datei geschrieben werden kann, sonst der Grund."""
        if self.dir is None:
            return "kein Ordner für die Zustandsdatei bekannt"
        probe = self.dir / f".schreibtest-{os.getpid()}"
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            probe.write_text("x", encoding="ascii")
            probe.unlink()
            return ""
        except OSError as e:
            try:
                probe.unlink()
            except OSError:
                pass
            return f"Zustandsdatei nicht schreibbar ({e.strerror or e})"

    @staticmethod
    def _valid(data) -> bool:
        """Streng: nur Werte, die ``claim`` schreibt.  (bool zählt nicht als Zahl.)"""
        if not isinstance(data, dict):
            return False
        pid, prev, fid = data.get("pid"), data.get("prev"), data.get("feature_id")
        if type(fid) is not int or fid != FEATURE_ID_FAN_FULL_SPEED:
            return False
        if type(prev) is not int or prev not in (FEATURE_VALUE_OFF, FEATURE_VALUE_ON):
            return False
        if type(pid) is not int or pid <= 0:
            return False
        ps = data.get("pstart")
        if ps is not None and (type(ps) is not int or ps <= 0):
            return False
        at = data.get("attempts", 0)
        if type(at) is not int or not (0 <= at <= 1000):
            return False
        tok = data.get("token")
        if tok is not None and not (isinstance(tok, str) and 0 < len(tok) <= 64):
            return False
        return True

    def read(self):
        """(Status, Daten): ('none', None) keine Datei; ('ok', dict); ('corrupt', None) unlesbar/ungültig."""
        path = self.path
        if path is None or not path.exists():
            return "none", None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "corrupt", None
        if self._valid(data):
            return "ok", data
        return "corrupt", None

    @staticmethod
    def _own_pstart():
        from . import converter  # spaet importiert (converter importiert dieses Modul); Startzeit-Funktion der Zielordner-Sperre

        return converter._proc_start_time(os.getpid())

    def claim(self, prev: int, feature_id: int) -> str:
        """Datei EXKLUSIV anlegen (schreiben, fsync).  Liefert die Kennung des Besitzers.  Wirft FileExistsError, wenn
        die Datei schon existiert, sonst OSError."""
        import uuid

        path = self.path
        if path is None:
            raise OSError("kein Ordner für die Zustandsdatei bekannt")
        self.dir.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        payload = json.dumps({"version": 2, "pid": os.getpid(), "pstart": self._own_pstart(), "token": token,
                              "feature_id": int(feature_id), "prev": int(prev), "attempts": 0,
                              "written": time.time()}).encode("utf-8")
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
        try:
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        _HELD_TOKENS.add(token)
        return token

    def _is_mine(self, token: Optional[str]) -> bool:
        """Gehört die Datei dem eigenen Besitzer (PID + Prozess-Startzeit + Kennung)?"""
        status, info = self.read()
        if status != "ok":
            return False
        return info["pid"] == os.getpid() and info.get("pstart") == self._own_pstart() and info.get("token") == token

    def release(self, token: Optional[str]) -> bool:
        """Nur die EIGENE Datei löschen.  True = gelöscht.  Fremde/fehlende Datei bleibt unangetastet.  Wirft OSError."""
        try:
            if token is not None and self._is_mine(token):
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    return False
                return True
            return False
        finally:
            _HELD_TOKENS.discard(token)

    def delete(self) -> None:
        """Datei ohne Besitzprüfung löschen (nur für beschädigte Dateien)."""
        path = self.path
        if path is None:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _same(a: dict, b: dict) -> bool:
        return all(a.get(k) == b.get(k) for k in ("pid", "pstart", "token", "written"))

    def delete_if_same(self, info: dict) -> None:
        """Datei löschen, wenn sie noch dieselbe ist wie ``info`` (ein anderer Lauf hat sie nicht inzwischen ersetzt)."""
        status, cur = self.read()
        if status == "ok" and self._same(cur, info):
            self.delete()

    def bump_attempts(self, info: dict) -> int:
        """Fehlversuch-Zähler der (noch gleichen) Datei erhöhen; liefert den neuen Stand."""
        n = int(info.get("attempts", 0)) + 1
        status, cur = self.read()
        if status != "ok" or not self._same(cur, info):
            return n
        cur["attempts"] = n
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(cur))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
        return n

    @staticmethod
    def owner_alive(info: dict) -> bool:
        """Lebt der Besitzerprozess (PID + Startzeit)?  Der EIGENE Prozess zählt nur als 'anderer Lauf', wenn die Kennung
        zu einem Anbieter gehört, der die Datei gerade hält.  Im Zweifel (Startzeit nicht ermittelbar) gilt ein
        existierender Prozess als lebend."""
        from . import converter

        pid = info.get("pid")
        if type(pid) is not int or pid <= 0:
            return False
        if pid == os.getpid():
            return info.get("token") in _HELD_TOKENS
        if not converter._pid_alive(pid):
            return False
        saved = info.get("pstart")
        if saved is not None:
            cur = converter._proc_start_time(pid)
            if cur is not None:
                return cur == saved  # andere Startzeit = PID wiederverwendet = Besitzer tot
        return True


# --------------------------------------------------------------------------- Lenovo Legion
_DENIED_WORDS = ("accessdenied", "access denied", "access is denied", "verweigert", "0x80041003", "unauthorized")
_NODEVICE_CODES = ("invalidnamespace", "invalidclass", "notfound", "notsupported")


def _is_denied(e: HelperRemoteError) -> bool:
    low = (e.code + " " + e.message).lower()
    return any(w in low for w in _DENIED_WORDS)


def _is_nodevice(e: HelperRemoteError) -> bool:
    low = (e.code + " " + e.message).lower()
    return e.code.lower() in _NODEVICE_CODES or "invalidclass" in low or "keine instanz" in low or "invalid namespace" in low


class LegionProvider(ThermalProvider):
    """Lenovo Legion über WMI (``root\\wmi``), Zugriff nur mit Administratorrechten.

    Messung: ``LENOVO_GAMEZONE_DATA.GetCPUTemp`` / ``GetFan1Speed`` / ``GetFan2Speed`` / ``GetFanMaxSpeed``.
    Lüfter voll: ausschliesslich über ``LENOVO_OTHER_METHOD.SetFeatureValue(IDs=FEATURE_ID_FAN_FULL_SPEED, value=1)``;
    ``GetFeatureValue`` bekommt NUR ``IDs`` (``value`` ist Ausgabe).  Ist die Kennung nicht lesbar, wird nicht geschaltet
    (nur messen/pausieren).  Der SmartFanMode wird nie geschaltet, nur gelesen (``read_smart_fan_mode``).
    Nach jedem Schreiben wird zurückgelesen.  ``trace(text)`` meldet jeden Einzelschritt (``--thermal-test``).

    Hängt oder stirbt der Hilfsprozess, wird ein frischer gestartet (Messung: höchstens ``MAX_HELPER_RESTARTS``-mal je
    Lauf; Zurückstellen: bis zu 2 Versuche)."""

    name = "Lenovo Legion (WMI)"

    def __init__(self, helper_factory: Optional[Callable] = None, trace: Optional[Callable] = None,
                 platform: Optional[str] = None, state_store: Optional[FanStateStore] = None) -> None:
        self._factory = helper_factory or PowerShellHelper
        self._trace = trace
        self._platform = platform or sys.platform
        self._store = state_store if state_store is not None else FanStateStore()
        self._helper = None
        self._hlock = threading.RLock()  # Austausch des Hilfsprozesses (nur kurz gehalten)
        self._rlock = threading.Lock()  # Neustarts des Hilfsprozesses nacheinander (auch während des Starts gehalten)
        self._aborted = False
        self._closing = False  # finish()/stop() läuft: kein Neustart des Hilfsprozesses mehr, Messung scheitert sofort
        self._restarts = 0  # Neustarts wegen Messfehlern in diesem Lauf
        self._token: Optional[str] = None  # Besitzerkennung der eigenen Zustandsdatei
        self._fan_mode = ""  # "feature" | ""
        self._fan_note = "Lüftersteuerung auf diesem Gerät nicht verfügbar"
        self._prev: Optional[int] = None  # Zustand VOR dem ersten Schalten (gelesen unmittelbar davor)
        self._state_written = False
        self._fan_max: Optional[int] = None
        self._fan_max_raw = None
        self._messages: list = []

    @property
    def fan_mode(self) -> str:
        return self._fan_mode

    @property
    def fan_max_raw(self):
        """Rohwert von GetFanMaxSpeed (Einheit unbekannt; None = nicht lesbar)."""
        return self._fan_max_raw

    def begin_close(self) -> None:
        """Der Lauf endet: kein Neustart des Hilfsprozesses mehr, Messungen scheitern sofort (das Zurückstellen
        bekommt den Hilfsprozess, ohne dass ein Mess-Thread ihn beendet oder neu startet)."""
        self._closing = True

    @property
    def previous_fan_state(self) -> Optional[int]:
        return self._prev

    def take_messages(self) -> list:
        msgs, self._messages = self._messages, []
        return msgs

    def _t(self, text: str) -> None:
        if self._trace is not None:
            try:
                self._trace(text)
            except Exception:  # noqa: BLE001
                pass

    # ---- Hilfsprozess
    def _restart_helper(self) -> None:
        """Frischen Hilfsprozess starten (alten beenden), unabhängig vom Zustand des alten.  Wirft HelperError."""
        with self._rlock:
            self._restart_locked()

    def _restart_locked(self) -> None:
        with self._hlock:
            if self._aborted:
                raise HelperError("Anbieter wurde beendet")
            old, self._helper = self._helper, None
            if old is not None:
                self._safe_close_fast(old)
            self._t("  Hilfsprozess wird neu gestartet ...")
            helper = self._factory()
            # schon VOR dem Start eintragen: abort() kann auch den noch startenden Hilfsprozess beenden
            self._helper = helper
        try:
            helper.start()
        except HelperError:
            with self._hlock:
                if self._helper is helper:
                    self._helper = None
            self._safe_close(helper)
            raise
        if self._aborted:
            with self._hlock:
                if self._helper is helper:
                    self._helper = None
            self._safe_close_fast(helper)
            raise HelperError("Anbieter wurde beendet")

    def _ensure_helper(self) -> None:
        with self._rlock:  # ein gerade laufender Neustart (anderer Thread) wird abgewartet, nicht überholt
            h = self._helper
            if h is None or not getattr(h, "alive", True):
                self._restart_locked()

    # ---- Aufruf
    def _call(self, cls: str, method: str, _timeout: Optional[float] = None, **params) -> dict:
        args = "".join(f" {k}={int(v)}" for k, v in params.items())
        shown = f"{cls}.{method}({', '.join(f'{k}={int(v)}' for k, v in params.items())})"
        helper = self._helper
        if helper is None:
            raise HelperError("Hilfsprozess nicht aktiv")
        try:
            if _timeout is not None:
                payload = helper.call(f"CALL {cls} {method}{args}", timeout=_timeout)
            else:
                payload = helper.call(f"CALL {cls} {method}{args}")
        except HelperRemoteError as e:
            self._t(f"  WMI {shown} -> FEHLER {e.code or '-'}: {e.message}")
            raise
        except ThermalError as e:
            self._t(f"  WMI {shown} -> {e}")
            raise
        out: dict = {}
        for tok in payload.split():
            k, _, v = tok.partition("=")
            try:
                out[k] = int(v)
            except ValueError:
                out[k] = v
        self._t(f"  WMI {shown} -> {payload or '(leer)'}")
        return out

    def _data(self, method: str, timeout: Optional[float] = None) -> int:
        r = self._call(WMI_CLASS_GAMEZONE, method, timeout)
        if not isinstance(r.get("Data"), int):
            raise ProviderError(f"{method}: keine Zahl 'Data' in der Antwort")
        return r["Data"]

    def _feature_get(self, feature_id: int = FEATURE_ID_FAN_FULL_SPEED, timeout: Optional[float] = None) -> int:
        # NUR IDs als Eingabe (laut Get-CimClass ist 'value' Ausgabe); 'value' steht in der Antwort
        r = self._call(WMI_CLASS_OTHER, "GetFeatureValue", timeout, IDs=feature_id)
        v = r.get("value")
        if v not in (FEATURE_VALUE_ON, FEATURE_VALUE_OFF):
            raise ProviderError(f"GetFeatureValue(0x{feature_id:08X}) lieferte {v!r}")
        return v

    def _feature_set(self, value: int, feature_id: int = FEATURE_ID_FAN_FULL_SPEED) -> None:
        # Unabhängig von der Herkunft der Werte (auch die Zustandsdatei ist von aussen veränderbar): geschrieben wird
        # ausschliesslich "Lüfter voll" mit 0 oder 1
        if type(feature_id) is not int or feature_id != FEATURE_ID_FAN_FULL_SPEED \
                or type(value) is not int or value not in (FEATURE_VALUE_OFF, FEATURE_VALUE_ON):
            raise ProviderError(f"unzulässiger Schreibzugriff abgelehnt (Kennung {feature_id!r}, Wert {value!r})")
        self._call(WMI_CLASS_OTHER, "SetFeatureValue", HELPER_WRITE_TIMEOUT_S, IDs=feature_id, value=value)

    def read_smart_fan_mode(self) -> Optional[int]:
        """SmartFanMode NUR LESEN (nur zur Anzeige in --thermal-test); None, wenn nicht lesbar."""
        try:
            return self._data("GetSmartFanMode")
        except ThermalError:
            return None

    # ---- Schnittstelle
    def open(self) -> None:
        if self._platform != "win32":
            raise ProviderUnavailable("Der Lenovo-Legion-Anbieter funktioniert nur unter Windows.", "platform")
        self._t("Starte versteckten PowerShell-Hilfsprozess ...")
        helper = self._factory()
        with self._hlock:  # schon vor dem Start eintragen, damit abort() ihn während des Starts beenden kann
            if self._aborted:
                self._safe_close(helper)
                raise ProviderUnavailable("Anbieter wurde beendet.", "other")
            self._helper = helper
        try:
            helper.start()
        except HelperError as e:
            with self._hlock:
                if self._helper is helper:
                    self._helper = None
            self._safe_close(helper)
            self._t(f"  PowerShell-Start gescheitert: {type(e).__name__}: {e}")
            raise ProviderUnavailable("Windows-PowerShell ist nicht nutzbar.", "powershell") from e
        try:
            self._open_probe()
        except BaseException:
            self.close()
            raise

    def _open_probe(self) -> None:
        try:
            cpu = self._data("GetCPUTemp")
        except HelperRemoteError as e:
            if _is_denied(e):
                raise ProviderUnavailable(
                    "Administratorrechte nötig (Zugriff auf die Lenovo-Hardwareabfrage verweigert)", "admin") from e
            if _is_nodevice(e):
                raise ProviderUnavailable("Kein Lenovo-Legion-Gerät erkannt", "nodevice") from e
            self._t(f"  Temperaturabfrage: {e.code or '-'}: {e.message}")
            raise ProviderUnavailable("Die Temperaturabfrage ist fehlgeschlagen", "other") from e
        except ThermalError as e:
            self._t(f"  Temperaturabfrage: {type(e).__name__}: {e}")
            raise ProviderUnavailable("Die Temperaturabfrage ist fehlgeschlagen", "other") from e
        if not plausible(cpu):
            self._t(f"  Unplausible Temperatur: {cpu} °C")
            raise ProviderUnavailable("Die gemessene Temperatur ist unplausibel", "implausible")
        try:
            self._fan_max_raw = self._data("GetFanMaxSpeed")
            self._fan_max = self._fan_max_raw
        except ThermalError:
            self._fan_max = None
        self._probe_fan_control()

    def _probe_fan_control(self) -> None:
        """Lüfterschaltung nur LESEND prüfen (kein Schreibaufruf ausser der Wiederherstellung nach einem
        abgebrochenen Lauf): Kennung lesbar?  Zustandsdatei schreibbar?  Anderer Lauf aktiv?"""
        self._fan_mode = ""
        try:
            self._feature_get()
        except HelperError as e:
            self._t(f"  Hilfsprozess ausgefallen: {e}")
            raise ProviderUnavailable("Die Temperaturabfrage über PowerShell ist ausgefallen", "powershell") from e
        except ThermalError as e:
            self._fan_note = "Lüftersteuerung auf diesem Gerät nicht verfügbar"
            self._messages.append(("detail", f"Lüfterkennung 0x{FEATURE_ID_FAN_FULL_SPEED:08X} nicht lesbar: "
                                             f"{type(e).__name__}: {e}"))
            return
        why = self._store.check_writable()
        if why:
            self._fan_note = "Lüftersteuerung nicht verfügbar (die Zustandsdatei kann nicht geschrieben werden)"
            self._messages.append(("detail", f"Zustandsdatei: {why}"))
            return
        if not self._handle_leftover_state():
            return
        self._fan_mode = "feature"
        self._fan_note = (f"Lüfter voll über LENOVO_OTHER_METHOD (Kennung 0x{FEATURE_ID_FAN_FULL_SPEED:08X}, "
                          "Wirkung unbestätigt)")

    def _handle_leftover_state(self) -> bool:
        """Zustandsdatei auswerten.  False = es darf nicht geschaltet werden (Grund steht in ``_fan_note``)."""
        status, info = self._store.read()
        if status == "none":
            return True
        if status == "corrupt":
            return self._drop_corrupt()
        if FanStateStore.owner_alive(info):
            self._fan_note = "Lüftersteuerung wird von einem anderen Lauf benutzt"
            return False
        return self._restore_leftover(info)

    def _drop_corrupt(self) -> bool:
        """Unlesbare/ungültige Zustandsdatei löschen (NICHTS in die Hardware schreiben).  False = nicht löschbar."""
        self._messages.append(("warn", "Die Zustandsdatei der Lüftersteuerung war beschädigt oder ungültig und wurde "
                                       "gelöscht; ein evtl. noch auf 'voll' stehender Lüfter wird nicht zurückgestellt "
                                       "(bitte in Lenovo Vantage prüfen)."))
        self._messages.append(("detail", f"Zustandsdatei: {self._store.path}"))
        try:
            self._store.delete()
        except OSError as e:
            self._fan_note = f"Zustandsdatei nicht löschbar ({e.strerror or e})"
            return False
        return True

    def _restore_leftover(self, info: dict) -> bool:
        """Zustand eines toten Besitzers wiederherstellen.  False = gescheitert (es wird nicht geschaltet).  Nach
        ``RESTORE_MAX_ATTEMPTS`` Fehlversuchen (Zähler in der Datei) wird die Datei verworfen und gewarnt, damit die
        Steuerung nicht für immer gesperrt bleibt."""
        path = self._store.path
        try:
            self._feature_set(info["prev"], info["feature_id"])
            now = self._feature_get(info["feature_id"], HELPER_WRITE_TIMEOUT_S)
            if now != info["prev"]:
                raise ProviderError(f"zurückgelesen {now} statt {info['prev']}")
            self._store.delete_if_same(info)
        except (ThermalError, OSError) as e:
            n = self._store.bump_attempts(info)
            self._messages.append(("detail", f"Wiederherstellen aus {path} gescheitert (Versuch {n}): "
                                             f"{type(e).__name__}: {e}"))
            if n >= RESTORE_MAX_ATTEMPTS:
                try:
                    self._store.delete_if_same(info)
                except OSError:
                    pass
                self._messages.append(("warn", "Lüfterzustand konnte nicht wiederhergestellt werden; bitte in Lenovo "
                                               f"Vantage prüfen (nach {n} Versuchen wurde die Zustandsdatei verworfen)."))
                return True
            self._fan_note = (f"Lüfterzustand nach abgebrochenem Lauf nicht wiederherstellbar (Versuch {n} von "
                              f"{RESTORE_MAX_ATTEMPTS}; Datei: {path})")
            return False
        self._messages.append(("info", "Lüfterzustand nach abgebrochenem Lauf wiederhergestellt"))
        return True

    def measure(self) -> Messung:
        if self._closing or self._aborted:
            raise HelperError("Anbieter wird beendet")
        failed = self._helper
        try:
            return self._measure_once()
        except HelperBusy:
            raise  # ein anderer Aufruf (z. B. Zurückstellen) benutzt den Hilfsprozess: nicht anfassen, nicht neu starten
        except HelperError as e:
            if self._closing or self._aborted:
                raise
            # Zeitlimit/Absturz des Hilfsprozesses: neu starten und weitermessen (höchstens MAX_HELPER_RESTARTS je Lauf);
            # erst das wiederholte Scheitern gilt (beim Aufrufer) als Ausfall des Anbieters
            self._t(f"  Messung scheiterte ({e}); Hilfsprozess neu starten und erneut messen")
            with self._rlock:
                if self._closing or self._aborted:
                    raise
                cur = self._helper
                # hat ein anderer Thread (Zurückstellen) inzwischen schon einen frischen Hilfsprozess gestartet, wird er benutzt
                if cur is failed or cur is None or not getattr(cur, "alive", True):
                    if self._restarts >= MAX_HELPER_RESTARTS:
                        raise HelperError(f"Hilfsprozess musste schon {self._restarts}-mal neu gestartet werden") from e
                    self._restarts += 1
                    self._restart_locked()
            return self._measure_once()

    def _measure_once(self) -> Messung:
        cpu = self._data("GetCPUTemp")
        rpms = []
        for m in ("GetFan1Speed", "GetFan2Speed"):
            try:
                rpms.append(self._data(m))
            except HelperError:
                raise
            except ThermalError:
                pass  # Drehzahl ist nur Anzeige
        return Messung(float(cpu), tuple(rpms), self._fan_max)

    def fan_control_info(self) -> tuple:
        return bool(self._fan_mode), self._fan_note

    def fan_full(self) -> None:
        if self._fan_mode != "feature":
            raise ProviderError("keine Lüfterschaltung verfügbar")
        self._ensure_helper()
        if not self._state_written:
            self._claim_state()
        self._feature_set(FEATURE_VALUE_ON)
        now = self._feature_get(timeout=HELPER_WRITE_TIMEOUT_S)
        if now != FEATURE_VALUE_ON:
            raise ProviderError(f"Lüfter voll: zurückgelesen {now} statt {FEATURE_VALUE_ON}")

    def _claim_state(self) -> None:
        """Reihenfolge: erst den Vorzustand lesen, dann die Zustandsdatei EXKLUSIV anlegen, dann (beim Aufrufer) schalten.
        Scheitert das Anlegen, wird nicht geschaltet.  Existiert die Datei: Besitzer lebt -> nicht schalten
        (``FanControlBusy``); Besitzer tot -> wie beim Start wiederherstellen und neu versuchen."""
        for _ in range(2):
            prev = self._feature_get(timeout=HELPER_WRITE_TIMEOUT_S)
            try:
                token = self._store.claim(prev, FEATURE_ID_FAN_FULL_SPEED)
            except FileExistsError:
                status, info = self._store.read()
                if status == "none":
                    continue  # inzwischen gelöscht: noch einmal (Vorzustand neu lesen)
                if status == "corrupt":
                    if not self._drop_corrupt():
                        raise ProviderError("Zustandsdatei nicht löschbar; es wird nicht geschaltet") from None
                    continue
                if FanStateStore.owner_alive(info):
                    raise FanControlBusy("Lüftersteuerung wird von einem anderen Lauf benutzt") from None
                if not self._restore_leftover(info):
                    raise ProviderError("Lüfterzustand eines abgebrochenen Laufs nicht wiederherstellbar; "
                                        "es wird nicht geschaltet") from None
                continue
            except OSError as e:
                raise ProviderError(f"Zustandsdatei nicht schreibbar ({e.strerror or e}); es wird nicht geschaltet") from e
            self._prev = prev
            self._token = token
            self._state_written = True
            return
        raise FanControlBusy("Lüftersteuerung wird von einem anderen Lauf benutzt")

    def fan_restore(self) -> None:
        """Vorherigen Zustand wiederherstellen (bis zu 2 Versuche; ein toter Hilfsprozess wird dafür neu gestartet).
        Die Zustandsdatei wird nur gelöscht, wenn sie dem eigenen Lauf gehört."""
        if self._prev is None or not self._state_written:
            return
        err: Optional[Exception] = None
        for attempt in range(2):
            try:
                self._ensure_helper()
                self._feature_set(self._prev)
                now = self._feature_get(timeout=HELPER_WRITE_TIMEOUT_S)
                if now != self._prev:
                    raise ProviderError(f"Lüfter-Zustand zurückgelesen {now} statt {self._prev}")
                err = None
                break
            except ThermalError as e:
                err = e
                self._t(f"  Zurückstellen, Versuch {attempt + 1}: {e}")
        if err is not None:
            raise err if isinstance(err, ProviderError) else ProviderError(str(err))
        try:
            self._store.release(self._token)
        except OSError as e:
            self._messages.append(("warn", f"Zustandsdatei konnte nicht gelöscht werden: {e}"))
        self._prev = None
        self._token = None
        self._state_written = False

    @staticmethod
    def _safe_close(helper) -> None:
        try:
            helper.close()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _safe_close_fast(helper) -> None:
        try:
            helper.close(grace=0.0)
        except TypeError:
            try:
                helper.close()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    def abort(self) -> None:
        """Sofort aufgeben (aus einem anderen Thread): Hilfsprozess hart beenden, kein Neustart mehr."""
        self._aborted = True
        h = self._helper
        if h is not None:
            kill = getattr(h, "abort", None)
            if kill is not None:
                try:
                    kill()
                except Exception:  # noqa: BLE001
                    pass
            else:
                self._safe_close_fast(h)

    def close(self) -> None:
        with self._hlock:
            h, self._helper = self._helper, None
        if h is not None:
            self._safe_close(h)


# --------------------------------------------------------------------------- Verfügbarkeit
@dataclass
class Availability:
    ok: bool
    reason: str = ""
    code: str = ""
    provider: Optional[ThermalProvider] = None
    first: Optional[Messung] = None
    detail: str = ""  # technische Einzelheiten (nur Protokoll)


def check_availability(provider_factory: Optional[Callable] = None, holder: Optional[list] = None) -> Availability:
    """Anbieter öffnen und einmal messen.  Nie eine Ausnahme: bei Problemen ``ok=False`` mit deutschem Grund
    (anderes Gerät, keine Adminrechte, PowerShell fehlt, unplausible Messung ...).  ``holder``: Liste, in die der
    Anbieter sofort nach dem Erzeugen eingetragen wird (damit ein anderer Thread ihn per ``abort()`` beenden kann)."""
    prov = None
    try:
        prov = (provider_factory or LegionProvider)()
        if holder is not None:
            holder.append(prov)
        prov.open()
        m = prov.measure()
        if not plausible(m.cpu_temp_c):
            raise ProviderUnavailable("Die gemessene Temperatur ist unplausibel", "implausible")
        return Availability(True, provider=prov, first=m)
    except ProviderUnavailable as e:
        _safe_close(prov)
        return Availability(False, e.reason, e.code)
    except BaseException as e:  # noqa: BLE001
        _safe_close(prov)
        if not isinstance(e, Exception):
            raise
        return Availability(False, "Die Temperatur konnte nicht gemessen werden", "other",
                            detail=f"{type(e).__name__}: {e}")


def _safe_close(prov) -> None:
    if prov is not None:
        try:
            prov.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- Regler
@dataclass
class ThermalStats:
    pauses: int = 0
    pause_seconds: float = 0.0
    max_cpu_c: Optional[float] = None
    fan_full_count: int = 0
    unavailable_reason: str = ""  # nicht verfügbar (von Anfang an oder ausgefallen)
    fan_note: str = ""
    abort_reason: str = ""  # Pause zu lang: Lauf wurde abgebrochen


class ThermalController:
    """Hardwareunabhängiger Regler (mit Attrappen-Anbieter testbar).

    ``tick()`` = eine Messung + Entscheidung.  ``finish()`` stellt IMMER her: angehaltene Prozesse fortsetzen,
    Lüfterzustand zurück, Anbieter schliessen (idempotent, wirft nie).
    ``pause_ctl``: Objekt mit ``pause() -> bool`` und ``resume()``.  ``notify(level, text)`` für das Protokoll,
    ``status(text, state)`` für die Statuszeile (state: ok | fan | pause | unavailable).
    ``on_abort(grund)``: wird aufgerufen, wenn eine Pause länger als ``cfg.max_pause_s`` dauert (der Aufrufer bricht
    den Lauf dann sauber ab, fortsetzbar)."""

    def __init__(self, provider: ThermalProvider, config: Optional[ThermalConfig] = None, pause_ctl=None,
                 notify: Optional[Callable] = None, status: Optional[Callable] = None,
                 clock: Callable = time.monotonic, on_abort: Optional[Callable] = None) -> None:
        self.provider = provider
        self.cfg = config or ThermalConfig()
        self.pause_ctl = pause_ctl
        self._notify = notify
        self._status = status
        self._on_abort = on_abort
        self._clock = clock
        self.stats = ThermalStats()
        self.fan_full = False
        self.paused = False
        self.failed = ""
        self.aborted = ""
        self.fan_ok = False
        self.fan_reason = ""  # Grund, wenn die Lüfterschaltung nicht verfügbar ist
        self._fan_touched = False
        self._finished = False
        self._closing = False
        self._bad = 0
        self._up_fan = self._down_fan = self._up_pause = self._down_pause = 0
        self._pause_t0 = 0.0
        self._pause_log_t = 0.0
        self._pause_fail_logged = False
        # Wirkungsprüfung "Lüfter voll" (M1): "" | pending | confirmed | none | unknown
        self._fx = ""
        self._fx_before: Optional[float] = None
        self._fx_t0 = 0.0
        self._fx_tick = -1
        self._ticks = 0
        self._act = threading.RLock()
        self.last: Optional[Messung] = None
        _LIVE_CONTROLLERS.add(self)

    # ---- Meldungen (nie ein Fehler)
    def _n(self, level: str, text: str) -> None:
        if self._notify is not None:
            try:
                self._notify(level, text)
            except Exception:  # noqa: BLE001
                pass

    def _s(self, text: str, state: str) -> None:
        if self._status is not None:
            try:
                self._status(text, state)
            except Exception:  # noqa: BLE001
                pass

    def _drain(self) -> None:
        """Hinweise des Anbieters ins Protokoll übernehmen."""
        try:
            for level, text in self.provider.take_messages():
                self._n(level, text)
        except Exception:  # noqa: BLE001
            pass

    # ---- Ablauf
    def setup(self) -> None:
        try:
            self.fan_ok, note = self.provider.fan_control_info()
        except Exception as e:  # noqa: BLE001
            self.fan_ok, note = False, "Lüftersteuerung auf diesem Gerät nicht verfügbar"
            self._n("detail", f"Lüfterzustand nicht lesbar: {type(e).__name__}: {e}")
        self.stats.fan_note = str(note)
        self.fan_reason = "" if self.fan_ok else str(note)
        self._drain()
        self._n("info", f"Temperaturüberwachung aktiv ({getattr(self.provider, 'name', '?')}); "
                        f"Lüfter voll ab {self.cfg.fan_on_c:g} °C (zurück unter {self.cfg.fan_off_c:g}), "
                        f"Pause ab {self.cfg.pause_c:g} °C (weiter unter {self.cfg.resume_c:g}).")
        if self.fan_ok:
            self._n("info", f"Lüfterschaltung: {note}")
        else:
            self._n("warn", f"Lüftersteuerung nicht verfügbar: {note}. Es wird nur gemessen und bei Bedarf pausiert.")

    def tick(self, measurement: Optional[Messung] = None) -> None:
        """Eine Messung (oder die übergebene) auswerten.  Wirft nie."""
        if self._finished or self.failed or self.aborted:
            return
        try:
            m = measurement if measurement is not None else self.provider.measure()
        except Exception as e:  # noqa: BLE001
            if self._finished or self._closing:
                return  # der Lauf endet gerade: kein verspätetes "ausgefallen"
            self._fail("Die Temperaturmessung ist ausgefallen", detail=f"{type(e).__name__}: {e}")
            return
        try:
            self._evaluate(m)
        except Exception as e:  # noqa: BLE001
            self._fail("Fehler im Regler", detail=f"{type(e).__name__}: {e}")

    def _evaluate(self, m: Messung) -> None:
        with self._act:
            if self._finished or self.failed or self.aborted:
                return
            self._ticks += 1
            t = m.cpu_temp_c
            if not plausible(t):
                self._bad += 1
                if self._bad >= self.cfg.bad_limit:
                    self._fail("Die Temperaturmessung liefert unbrauchbare Werte", detail=f"{self._bad} unplausible Messungen in Folge (zuletzt {t} °C)")
                return
            self._bad = 0
            self.last = m
            c = self.cfg
            if self.stats.max_cpu_c is None or t > self.stats.max_cpu_c:
                self.stats.max_cpu_c = float(t)

            # --- Pause (Hysterese + Bestätigung)
            if not self.paused:
                self._up_pause = self._up_pause + 1 if t >= c.pause_c else 0
                if self._up_pause >= c.confirm_up:
                    self._do_pause(t)
            else:
                self._down_pause = self._down_pause + 1 if t < c.resume_c else 0
                if self._down_pause >= c.confirm_down:
                    self._do_resume(t)
                elif self._pause_progress(t):
                    return

            # --- Lüfter voll (Hysterese + Bestätigung); während der Pause immer voll
            if self.fan_ok:
                if not self.fan_full:
                    self._up_fan = self._up_fan + 1 if t >= c.fan_on_c else 0
                    if self._up_fan >= c.confirm_up or self.paused:
                        self._set_fan(True, t)
                else:
                    self._down_fan = self._down_fan + 1 if t < c.fan_off_c else 0
                    if self._down_fan >= c.confirm_down and not self.paused:
                        self._set_fan(False, t)
            self._check_fan_effect(m)
            self._s(format_status(m, self.fan_full, self.paused, c, self._fx or "pending", self.fan_reason),
                    "pause" if self.paused else ("fan" if self.fan_full else "ok"))

    # ---- Pause
    def _do_pause(self, t: float) -> None:
        ctl = self.pause_ctl
        ok = False
        if ctl is not None:
            ok = bool(ctl.pause())
        if not ok:
            self._up_pause = 0
            err = getattr(ctl, "pause_error", "") if ctl is not None else ""
            if err and not self._pause_fail_logged:
                self._pause_fail_logged = True  # nur einmal melden, solange es nicht klappt
                self._n("warn", f"Pause wegen Temperatur ({t:.0f} °C) nicht möglich: {err}.")
            return
        self._pause_fail_logged = False
        self.paused = True
        self._pause_t0 = self._pause_log_t = self._clock()
        self._down_pause = 0
        self.stats.pauses += 1
        self._n("warn", f"Pause wegen Temperatur ({t:.0f} °C, Schwelle {self.cfg.pause_c:g}): ffmpeg angehalten, "
                        f"weiter unter {self.cfg.resume_c:g} °C.")

    def _pause_progress(self, t: float) -> bool:
        """Während einer Pause: alle 2 Minuten eine Protokollzeile; nach der Höchstdauer Lauf abbrechen.
        True = Lauf wird abgebrochen."""
        now = self._clock()
        elapsed = now - self._pause_t0
        if elapsed >= self.cfg.max_pause_s:
            self._overheat_abort(t, elapsed)
            return True
        if now - self._pause_log_t >= self.cfg.pause_log_s:
            self._pause_log_t = now
            self._n("warn", f"Pause dauert seit {fmt_span(elapsed)}, CPU {t:.0f} °C (weiter unter {self.cfg.resume_c:g} °C).")
        return False

    def _overheat_abort(self, t: float, elapsed: float) -> None:
        reason = (f"Abgebrochen: CPU kühlt nicht ab ({t:.0f} °C nach {fmt_span(elapsed)} Pause). Rechner abkühlen "
                  "lassen, Lüftung prüfen, dann erneut starten (setzt fort); ggf. 'CPU-Kerne begrenzen' einstellen.")
        self.aborted = reason
        self.stats.abort_reason = reason
        self._end_pause()  # Zeit zählen und fortsetzen, dann sauber abbrechen (wie ein Benutzer-Abbruch)
        self._s(f"CPU {t:.0f} °C - {reason}", "unavailable")
        if self._on_abort is not None:
            try:
                self._on_abort(reason)
            except Exception as e:  # noqa: BLE001
                self._n("warn", f"Abbruch wegen Überhitzung konnte nicht ausgelöst werden: {e}")

    def _do_resume(self, t: float) -> None:
        self._end_pause()
        self._n("info", f"Pause beendet ({t:.0f} °C): ffmpeg läuft weiter.")

    def _end_pause(self) -> None:
        if not self.paused:
            return
        self.paused = False
        self._up_pause = self._down_pause = 0
        self.stats.pause_seconds += max(0.0, self._clock() - self._pause_t0)
        try:
            if self.pause_ctl is not None:
                self.pause_ctl.resume()
        except Exception as e:  # noqa: BLE001
            self._n("warn", f"Fortsetzen nach der Pause meldete einen Fehler: {e}")

    # ---- Lüfter
    @staticmethod
    def _top_rpm(m: Optional[Messung]) -> Optional[float]:
        if m is None or not m.fan_rpm:
            return None
        try:
            v = max(float(r) for r in m.fan_rpm)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    def _set_fan(self, on: bool, t: float) -> None:
        if on == self.fan_full:
            return
        self._up_fan = self._down_fan = 0
        if on:
            self._fan_touched = True  # ab jetzt muss wiederhergestellt werden - auch wenn der Aufruf mittendrin scheitert
        before = self._top_rpm(self.last)  # Drehzahl VOR dem Schalten
        try:
            if on:
                self.provider.fan_full()
            else:
                self.provider.fan_restore()
        except Exception as e:  # noqa: BLE001
            self.fan_ok = False
            if isinstance(e, FanControlBusy):
                self.fan_reason = "Lüftersteuerung wird von einem anderen Lauf benutzt"
                self._n("warn", f"{self.fan_reason}; es wird nur gemessen und bei Bedarf pausiert.")
            else:
                self.fan_reason = "Lüfter konnten nicht geschaltet werden"
                self._n("warn", "Die Lüfter konnten nicht auf voll geschaltet werden; es wird nicht weiter geschaltet.")
            self._n("detail", f"Lüfterschaltung: {type(e).__name__}: {e}")
            self._drain()
            self._restore_fan()
            return
        self._drain()
        self.fan_full = on
        if on:
            self.stats.fan_full_count += 1
            near = before is not None and fans_near_max(before, self.last.fan_max_rpm if self.last else None)
            self._fx = "nearmax" if near else ("pending" if before is not None else "unknown")
            self._fx_before, self._fx_t0, self._fx_tick = before, self._clock(), self._ticks
            if near:
                self._n("info", f"Lüfter voll angefordert ({t:.0f} °C); die Lüfter laufen bereits nahe Maximum "
                                f"({before:.0f} U/min), eine Wirkung ist nicht messbar.")
            elif before is None:
                self._n("info", f"Lüfter voll angefordert ({t:.0f} °C); Drehzahlen nicht lesbar, Wirkung nicht prüfbar.")
            else:
                self._n("info", f"Lüfter voll angefordert ({t:.0f} °C); Wirkung wird an der Drehzahl geprüft.")
        else:
            self._fan_touched = False
            self._fx = ""
            self._n("info", f"Lüfter zurück auf vorherigen Zustand ({t:.0f} °C).")

    def _check_fan_effect(self, m: Messung) -> None:
        """Beobachtet nach dem Schalten die Drehzahl (Regel: siehe ``fan_effect_confirmed``).  'Lüfter voll' wird nur
        angezeigt, wenn sie wirklich gestiegen ist; sonst steht dort 'angefordert' (einmal eine Protokollzeile)."""
        if not self.fan_full or self._fx != "pending" or self._fx_tick == self._ticks:
            return
        now = self._top_rpm(m)
        expired = self._clock() - self._fx_t0 >= self.cfg.fan_effect_window_s
        if now is not None and fan_effect_confirmed(self._fx_before, now, m.fan_max_rpm):
            self._fx = "confirmed"
            self._n("info", f"Lüfter voll wirkt (Drehzahl von {self._fx_before or 0:.0f} auf {now:.0f} U/min).")
        elif expired and now is None:
            self._fx = "unknown"
            self._n("info", "Lüfter voll angefordert (Drehzahlen nicht lesbar, Wirkung nicht prüfbar).")
        elif expired:
            self._fx = "none"
            self._n("warn", f"Lüfter voll angefordert, aber keine Wirkung messbar (Drehzahl {self._fx_before or 0:.0f} -> "
                            f"{now:.0f} U/min nach {self.cfg.fan_effect_window_s:g} s).")

    def _restore_fan(self) -> None:
        if not self._fan_touched:
            return
        err = None
        for _ in range(2):
            try:
                self.provider.fan_restore()
                err = None
                break
            except Exception as e:  # noqa: BLE001
                err = e
        self._drain()
        self.fan_full = False
        self._fx = ""
        if err is None:
            self._fan_touched = False
        else:
            self._n("detail", f"Zurückstellen der Lüfter gescheitert: {type(err).__name__}: {err}")
            self._n("warn", "Lüfterzustand konnte NICHT wiederhergestellt werden. Der nächste Lauf stellt ihn "
                            "wieder her (gespeicherter Zustand); sonst die Lüfter von Hand zurücksetzen "
                            "(z. B. Leistungsmodus in Lenovo Vantage/Legion Space).")

    def _fail(self, reason: str, detail: str = "") -> None:
        """Anbieter ausgefallen: alles zurücknehmen, die Umwandlung läuft unverändert weiter.  ``reason`` ist der
        Grund für den Anwender; ``detail`` (Ausnahme, Rohtext) steht nur im Protokoll."""
        with self._act:
            if self.failed or self._finished:
                return  # (nach finish() keine verspätete Meldung "ausgefallen")
            self.failed = reason
            self.stats.unavailable_reason = reason
            self._end_pause()
            self._restore_fan()
        if detail:
            self._n("detail", f"Temperaturüberwachung: Einzelheiten zum Ausfall: {detail}")
        self._n("warn", f"Temperaturüberwachung ausgefallen: {reason}. Die Umwandlung läuft ohne Überwachung weiter.")
        self._s(f"Temperatur nicht verfügbar: {reason}", "unavailable")

    def finish(self) -> None:
        """Pause beenden, Lüfter wiederherstellen, Anbieter schliessen.  Idempotent, wirft nie."""
        self._closing = True
        try:
            self.provider.begin_close()  # vor der Sperre: Messungen scheitern ab jetzt sofort, kein Neustart des Hilfsprozesses
        except Exception:  # noqa: BLE001
            pass
        try:
            with self._act:
                if self._finished:
                    return
                self._finished = True
                self._end_pause()
                self._restore_fan()
        except Exception:  # noqa: BLE001
            pass
        finally:
            _safe_close(self.provider)


# --------------------------------------------------------------------------- Sitzung (Thread)
class ThermalSession:
    """Verfügbarkeitsprüfung + Messthread für einen Lauf.  ``start()`` kehrt sofort zurück (das Starten von
    PowerShell dauert Sekunden); ``stop()`` stellt alles wieder her und wirft nie.  Nach ``stop()`` kommt keine
    verspätete Status-/Protokollzeile mehr."""

    def __init__(self, config: ThermalConfig, pause_ctl, notify: Optional[Callable] = None,
                 status: Optional[Callable] = None, provider_factory: Optional[Callable] = None,
                 interval_s: float = DEFAULT_INTERVAL_S, stop_wait_s: float = 20.0,
                 on_abort: Optional[Callable] = None) -> None:
        self.cfg = config
        self.pause_ctl = pause_ctl
        self._notify = notify
        self._status = status
        self._on_abort = on_abort
        self._factory = provider_factory
        self.interval_s = max(0.01, float(interval_s))
        self.stop_wait_s = stop_wait_s
        self._stop = threading.Event()
        self._silent = False
        self._thread: Optional[threading.Thread] = None
        self._holder: list = []
        self.controller: Optional[ThermalController] = None
        self.availability: Optional[Availability] = None
        self._stats = ThermalStats()

    @property
    def stats(self) -> ThermalStats:
        return self.controller.stats if self.controller is not None else self._stats

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="thermal", daemon=True)
        self._thread.start()

    def _n(self, level: str, text: str) -> None:
        if self._notify is not None and not self._silent:
            try:
                self._notify(level, text)
            except Exception:  # noqa: BLE001
                pass

    def _s(self, text: str, state: str) -> None:
        if self._status is not None and not self._silent:
            try:
                self._status(text, state)
            except Exception:  # noqa: BLE001
                pass

    def _run(self) -> None:
        ctl: Optional[ThermalController] = None
        try:
            av = check_availability(self._factory, self._holder)
            self.availability = av
            if self._stop.is_set():  # Lauf ist schon zu Ende: nichts mehr melden, Anbieter freigeben
                if av.provider is not None:
                    _safe_close(av.provider)
                return
            if not av.ok:
                self._stats.unavailable_reason = av.reason
                if av.detail:
                    self._n("detail", f"Temperatur nicht verfügbar - Einzelheiten: {av.detail}")
                self._n("info", f"Temperatur nicht verfügbar: {av.reason}. Die Umwandlung läuft unverändert.")
                self._s(f"Temperatur nicht verfügbar: {av.reason}", "admin" if av.code == "admin" else "unavailable")
                return
            ctl = ThermalController(av.provider, self.cfg, self.pause_ctl, self._n, self._s, on_abort=self._on_abort)
            self.controller = ctl
            ctl.setup()
            if self._stop.is_set():
                return
            ctl.tick(av.first)
            while not ctl.failed and not ctl.aborted and not self._stop.wait(self.interval_s):
                ctl.tick()
        except BaseException as e:  # noqa: BLE001
            if self._stop.is_set():
                return  # der Lauf endet ohnehin: keine verspätete Meldung
            self._stats.unavailable_reason = "Die Temperaturüberwachung wurde beendet"
            self._n("warn", "Die Temperaturüberwachung wurde unerwartet beendet; die Umwandlung läuft weiter.")
            self._n("detail", f"Temperaturüberwachung beendet: {type(e).__name__}: {e}")
        finally:
            if ctl is not None:
                ctl.finish()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        wait = self.stop_wait_s
        if self.controller is None:
            # Es wurde noch nichts geschaltet: laufende Verfügbarkeitsprüfung sofort abbrechen (Hilfsprozess beenden),
            # damit das Programmende nicht bis zu 20 s wartet
            for prov in list(self._holder):
                try:
                    prov.abort()
                except Exception:  # noqa: BLE001
                    pass
            wait = min(wait, 3.0)
        if t is not None and t is not threading.current_thread():
            t.join(timeout=wait)
        if self.controller is not None:
            self.controller.finish()  # idempotent; falls der Thread hängt (z. B. in einer Messung)
        self._silent = True


def shutdown_all(timeout: float = 5.0) -> None:
    """Notfall-Aufräumen (atexit, hartes Strg+C): alle Regler beenden (Pause, Lüfter wiederherstellen), alle
    Hilfsprozesse beenden.  Wirft nie, blockiert höchstens ``timeout`` Sekunden je Teil."""
    for ctl in list(_LIVE_CONTROLLERS):
        done = threading.Thread(target=ctl.finish, daemon=True)
        done.start()
        done.join(timeout)
    for h in list(_LIVE_HELPERS):
        try:
            h.close(grace=0.5)
        except Exception:  # noqa: BLE001
            pass


atexit.register(shutdown_all)


# --------------------------------------------------------------------------- Diagnose (--thermal-test)
@contextlib.contextmanager
def sigint_ignored():
    """Strg+C für die Dauer des Blocks ignorieren (Signalbehandlung umstellen und wiederherstellen).  Ausserhalb des
    Hauptthreads (signal.signal nicht erlaubt) passiert nichts."""
    old = None
    changed = False
    try:
        old = signal.signal(signal.SIGINT, signal.SIG_IGN)
        changed = True
    except (ValueError, OSError):
        pass
    try:
        yield
    finally:
        if changed:
            try:
                signal.signal(signal.SIGINT, old if old is not None else signal.SIG_DFL)
            except (ValueError, OSError):
                pass


def _fmt_rpm(m: Optional[Messung]) -> str:
    return "/".join(str(int(r)) for r in m.fan_rpm) + " U/min" if m is not None and m.fan_rpm else "nicht lesbar"


def fan_max_for_check(last: Optional[Messung], during: list):
    return next((x.fan_max_rpm for x in reversed(during) if x.fan_max_rpm), last.fan_max_rpm if last else None)


def run_thermal_test(out: Callable = print, provider_factory: Optional[Callable] = None,
                     sleep: Callable = time.sleep, full_seconds: float = 10.0, samples: int = 5,
                     input_fn: Optional[Callable] = None) -> int:
    """Diagnose für Laien: Anbieter, Rechte, Messungen, dann (nach Enter) Lüfter voll schalten, die Drehzahl vorher /
    während / nachher ausgeben, zurückstellen und ein klares Urteil ('ERGEBNIS: ...') als letzte Zeile.
    Strg+C bricht freundlich ab (die Lüfter werden dann zurückgestellt, Strg+C ist dabei gesperrt).
    Rückgabe 0 = Test lief (auch 'nur Messung möglich'), 1 = Problem/Abbruch.  Der SmartFanMode wird nur angezeigt."""
    input_fn = input_fn or input
    out("=== Temperatur-Diagnose (--thermal-test) ===")
    out(f"Plattform: {sys.platform}; Administratorrechte: {'ja' if is_admin() else 'NEIN'}")
    out(f"WICHTIG: Die Kennung für 'Lüfter voll' (0x{FEATURE_ID_FAN_FULL_SPEED:08X}) ist UNBESTÄTIGT (eine Vermutung aus "
        "einem Open-Source-Projekt). Dieser Test soll am Gerät zeigen, ob sie wirkt.")
    if provider_factory is None:
        def provider_factory():  # noqa: E306
            return LegionProvider(trace=out)
    out("Schritt 1: Anbieter öffnen, Temperatur lesen")
    prov = None
    problems = 0
    switched = False
    try:
        prov = provider_factory()
        try:
            prov.open()
        except ProviderUnavailable as e:
            out(f"  -> NICHT VERFÜGBAR: {e.reason}")
            if e.code == "admin":
                out("  Hinweis: 'Temperatur-Test (Administrator).bat' bzw. eine Administrator-Konsole benutzen.")
            out("ERGEBNIS: nicht verfügbar - es wurde nichts geschaltet")
            return 1
        out(f"  Anbieter: {getattr(prov, 'name', '?')} - geöffnet")
        smart = getattr(prov, "read_smart_fan_mode", None)
        if smart is not None:
            mode = smart()
            out("  SmartFanMode (nur gelesen, wird NIE geschaltet): "
                + (f"{mode} (1 leise, 2 ausgewogen, 3 Leistung, 255 benutzerdefiniert)" if mode is not None else "nicht lesbar"))
        raw_max = getattr(prov, "fan_max_raw", None)
        out(f"  GetFanMaxSpeed (Rohwert, Einheit unbekannt): {raw_max if raw_max is not None else 'nicht lesbar'}"
            + ("" if raw_max is None or plausible_fan_max(raw_max) else "  <- unplausibel, wird nicht verwendet"))
        out(f"Schritt 2: {samples} Messungen")
        last: Optional[Messung] = None
        for i in range(samples):
            try:
                m = prov.measure()
                ok = plausible(m.cpu_temp_c)
                out(f"  Messung {i + 1}: CPU {m.cpu_temp_c:g} °C, Lüfter {_fmt_rpm(m)}" + ("" if ok else "  <- UNPLAUSIBEL"))
                if ok:
                    last = m
                else:
                    problems += 1
            except ThermalError as e:
                out(f"  Messung {i + 1}: FEHLER {e}")
                problems += 1
            if i + 1 < samples:
                sleep(1.0)
        can, note = prov.fan_control_info()
        out(f"Schritt 3: Lüfterschaltung - {note}")
        if not can:
            out("  -> Es wird NICHT geschaltet (nur Messung).")
            out("ERGEBNIS: nur Messung möglich" + (f" ({problems} Problem(e) bei den Messungen)" if problems else ""))
            return 1 if problems else 0
        before = ThermalController._top_rpm(last)
        out(f"  Drehzahl VORHER: {_fmt_rpm(last)}")
        out(f"  Gleich werden die Lüfter {full_seconds:g} s lang auf voll geschaltet und danach zurückgestellt.")
        try:
            input_fn("Enter drücken zum Fortfahren (Strg+C = abbrechen) ")
        except (KeyboardInterrupt, EOFError):
            out("")
            out("Abgebrochen - es wurde nichts geschaltet.")
            out("ERGEBNIS: abgebrochen (nichts geschaltet)")
            return 1
        interrupted = False
        during: list = []
        err_text = ""
        try:
            try:
                switched = True
                prov.fan_full()
                out("  -> Lüfter voll: geschrieben und zurückgelesen (siehe oben)")
                t_end = time.monotonic() + full_seconds
                while time.monotonic() < t_end:
                    sleep(min(2.0, max(0.0, t_end - time.monotonic())))
                    try:
                        m = prov.measure()
                        during.append(m)
                        out(f"  während voll: CPU {m.cpu_temp_c:g} °C, Lüfter {_fmt_rpm(m)}")
                    except ThermalError as e:
                        out(f"  während voll: FEHLER {e}")
            except KeyboardInterrupt:
                interrupted = True
                out("")
                out("Abgebrochen (Strg+C) - die Lüfter werden jetzt zurückgestellt, bitte kurz warten ...")
            except ThermalError as e:
                err_text = str(e)
                out(f"  -> FEHLER beim Schalten: {e}")
                problems += 1
        finally:
            if switched:
                with sigint_ignored():  # ein zweites Strg+C darf das Zurückstellen nicht unterbrechen
                    out("Schritt 4: Lüfterzustand wiederherstellen")
                    try:
                        prov.fan_restore()
                        out("  -> wiederhergestellt: geschrieben und zurückgelesen (siehe oben)")
                    except ThermalError as e:
                        out(f"  -> FEHLER beim Wiederherstellen: {e}  (Lüfter von Hand zurücksetzen!)")
                        problems += 1
                    after = None
                    try:
                        after = prov.measure()
                        out(f"  Drehzahl NACHHER: CPU {after.cpu_temp_c:g} °C, Lüfter {_fmt_rpm(after)}")
                    except ThermalError:
                        pass
        if interrupted:
            out("ERGEBNIS: abgebrochen (Lüfter wurden zurückgestellt)" if not problems
                else "ERGEBNIS: abgebrochen, aber das Zurückstellen meldete einen Fehler - Lüfter prüfen!")
            return 1
        near_before = fans_near_max(before, fan_max_for_check(last, during))
        top_during = max([r for r in (ThermalController._top_rpm(x) for x in during) if r is not None], default=None)
        fan_max = fan_max_for_check(last, during)
        if near_before:
            out(f"ERGEBNIS: Die Lüfter liefen schon vorher nahe Maximum ({before:.0f} U/min) - eine Wirkung von 'Lüfter voll' "
                "ist so nicht messbar (Test bei kühlem Rechner wiederholen)" + ("" if not problems else f" - {problems} Problem(e), siehe oben"))
        elif top_during is not None and fan_effect_confirmed(before, top_during, fan_max):
            out(f"ERGEBNIS: Lüfter voll WIRKT (Drehzahl von {before or 0:.0f} auf {top_during:.0f} U/min)"
                + ("" if not problems else f" - aber {problems} Problem(e), siehe oben"))
        else:
            why = f" ({err_text})" if err_text else (" (Drehzahlen nicht lesbar)" if top_during is None or before is None else "")
            out(f"ERGEBNIS: KEINE Wirkung messbar{why} - bitte diese Ausgabe an den Autor zurückmelden")
        return 1 if problems else 0
    except KeyboardInterrupt:
        out("")
        if switched:
            out("ERGEBNIS: abgebrochen (die Lüfter wurden zurückgestellt)")
        else:
            out("ERGEBNIS: abgebrochen (es wurde nichts geschaltet)")
        return 1
    finally:
        _safe_close(prov)
