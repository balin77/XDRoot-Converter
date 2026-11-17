"""Energiesparmodus (Standby/Ruhezustand) waehrend eines Laufs verhindern (nur Windows).

``keep_awake()`` setzt ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)``: Windows geht dann wegen
Inaktivitaet nicht in den Standby/Ruhezustand.  Der Bildschirm darf ausgehen (kein ``ES_DISPLAY_REQUIRED``, kein
``ES_AWAYMODE_REQUIRED``).  Beim Verlassen wird ``ES_CONTINUOUS`` allein gesetzt (= Anforderung zurueckgenommen).

Der Zustand gilt je THREAD.  Eintritt und Verlassen laufen deshalb im selben Thread: der Kontextmanager wird im
Laufrahmen des Converters (``Converter.run``, Arbeits-Thread der GUI) bzw. im ``--verify``-Weg der Kommandozeile
benutzt, nie in der Oberflaeche.  Verschachtelung im selben Thread ist harmlos: nur der aeusserste Eintritt setzt
und nimmt zurueck.

Was das NICHT verhindert: Zuklappen des Deckels, Druck auf den Netzschalter/Ruhezustand-Taste, kritischer
Akkustand und manuelles "Energie sparen"/"Herunterfahren" (Startmenue).  Auch Abmeldung oder Neustart durch Updates
bleiben moeglich.  Auf anderen Systemen und bei jedem Fehler passiert nichts; der Lauf wird nie gestoert.
"""
from __future__ import annotations

import contextlib
import sys
import threading
from typing import Callable, Iterator, Optional

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

MSG_ACTIVE = "Energiesparmodus wird während des Laufs verhindert (Bildschirm darf ausgehen)."
MSG_FAILED = "Hinweis: Energiesparmodus konnte nicht verhindert werden – {why}"

_local = threading.local()


def _set_thread_execution_state(flags: int) -> int:
    """Kernel-Aufruf; gibt den vorherigen Zustand zurueck, 0 bei Fehler.  Wirft bei fehlendem windll."""
    import ctypes
    fn = ctypes.windll.kernel32.SetThreadExecutionState  # type: ignore[attr-defined]
    fn.restype = ctypes.c_uint32
    fn.argtypes = [ctypes.c_uint32]
    return int(fn(ctypes.c_uint32(flags)))


def is_active() -> bool:
    """Hält dieser Thread die Anforderung gerade (aeusserer Eintritt aktiv)?"""
    return bool(getattr(_local, "depth", 0) > 0 and getattr(_local, "active", False))


def _say(log: Optional[Callable[[str], None]], msg: str) -> None:
    if log is not None:
        try:
            log(msg)
        except Exception:  # noqa: BLE001
            pass


@contextlib.contextmanager
def keep_awake(log: Optional[Callable[[str], None]] = None) -> Iterator[bool]:
    """Kontextmanager; liefert True, wenn die Anforderung aktiv ist.  Siehe Moduldocstring."""
    depth = getattr(_local, "depth", 0)
    if depth > 0:  # verschachtelt: aeusserer Eintritt im selben Thread haelt den Zustand
        _local.depth = depth + 1
        try:
            yield True
        finally:
            _local.depth -= 1
        return
    active = False
    if sys.platform != "win32":
        _say(log, MSG_FAILED.format(why="nur unter Windows verfügbar."))
    else:
        try:
            if _set_thread_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) != 0:
                active = True
                _say(log, MSG_ACTIVE)
            else:
                _say(log, MSG_FAILED.format(why="Windows hat die Anforderung abgelehnt."))
        except Exception as e:  # noqa: BLE001
            _say(log, MSG_FAILED.format(why=f"{type(e).__name__}: {e}"))
    _local.depth = 1
    _local.active = active
    try:
        yield active
    finally:
        _local.depth = 0
        _local.active = False
        if active:
            try:
                _set_thread_execution_state(ES_CONTINUOUS)
            except Exception:  # noqa: BLE001
                pass
