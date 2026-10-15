"""Turn what the MKS WIFI module sends back into data.

The module answers each query with "ok" and a reply line, in no guaranteed
order, and interleaves temperature lines whenever it likes. Lines end with
"\\r\\n" or a bare "\\n". So the parser looks at every line on its own and
ignores anything it does not recognise.
"""

from __future__ import annotations

import re
from datetime import timedelta

from .models import PrinterState, PrinterStatus, Temperature

# Some firmwares put "ok" in front of the reply on the same line.
_OK_PREFIX = re.compile(r"^(?:ok\b\s*)+", re.IGNORECASE)
_NUMBER = r"-?\d+(?:\.\d+)?"
# "T:200 /200", "T0:199.5 /200.0", "B:59 /60". The lookbehind skips "B@:0".
_TEMPERATURE = re.compile(rf"(?<![A-Za-z0-9@])(T\d*|B):\s*({_NUMBER})\s*/\s*({_NUMBER})")
_STATE = re.compile(r"^M997\b\s*(\S*)", re.IGNORECASE)
_PROGRESS = re.compile(r"^M27\b\s*(\d+)?", re.IGNORECASE)
_FILE = re.compile(r"^M994\b\s*(.*)$", re.IGNORECASE)
_ELAPSED = re.compile(r"^M992\b\s*(?:(\d+):(\d{1,2}):(\d{1,2}))?", re.IGNORECASE)
_VOLUME = re.compile(r"^\d+:/?")
_FIRMWARE = re.compile(r"FIRMWARE_NAME:\s*(.*?)\s*(?=\s[A-Z][A-Z0-9_]*:|$)")

_STATES = {
    "IDLE": PrinterState.IDLE,
    "PRINTING": PrinterState.PRINTING,
    "PAUSE": PrinterState.PAUSED,
    "PAUSED": PrinterState.PAUSED,
}


def _lines(text: str) -> list[str]:
    return [_OK_PREFIX.sub("", line.strip()) for line in text.splitlines()]


def _temperatures(line: str) -> dict[str, Temperature]:
    return {
        name: Temperature(current=float(current), target=float(target))
        for name, current, target in _TEMPERATURE.findall(line)
    }


def replied_to(text: str) -> frozenset[str]:
    """Which queries ("M105", "M997", "M27", "M994", "M992", "M115") got an answer in text."""
    seen: set[str] = set()
    for line in _lines(text):
        if _temperatures(line):
            seen.add("M105")
        if _FIRMWARE.search(line):
            seen.add("M115")
        for command, pattern in (
            ("M997", _STATE),
            ("M27", _PROGRESS),
            ("M994", _FILE),
            ("M992", _ELAPSED),
        ):
            if pattern.match(line):
                seen.add(command)
    return frozenset(seen)


def parse_status(text: str) -> PrinterStatus:
    """Build a PrinterStatus from the replies to M105, M997, M27, M994 and M992.

    Replies can come in any order, mixed with unsolicited temperature lines;
    the last temperature line wins. Missing replies leave their fields None.
    """
    state = PrinterState.UNKNOWN
    temperatures: dict[str, Temperature] = {}
    progress: int | None = None
    file_name: str | None = None
    file_size: int | None = None
    elapsed: timedelta | None = None

    for line in _lines(text):
        if found := _temperatures(line):
            temperatures = found
        elif match := _STATE.match(line):
            state = _STATES.get(match[1].upper(), PrinterState.UNKNOWN)
        elif match := _PROGRESS.match(line):
            progress = int(match[1]) if match[1] else None
        elif match := _FILE.match(line):
            # "1:/stacked.gcode;2013404224": volume, path, size in bytes.
            name, separator, size = match[1].rpartition(";")
            if not separator:
                name, size = size, ""
            file_name = _VOLUME.sub("", name.strip()) or None
            file_size = int(size) if size.strip().isdigit() else None
        elif match := _ELAPSED.match(line):
            hours, minutes, seconds = match.groups()
            elapsed = (
                timedelta(hours=int(hours), minutes=int(minutes), seconds=int(seconds))
                if hours is not None
                else None
            )

    nozzles = {
        int(name[1:]): temperature
        for name, temperature in temperatures.items()
        if name.startswith("T") and len(name) > 1
    }
    if 0 not in nozzles and "T" in temperatures:
        nozzles[0] = temperatures["T"]

    return PrinterStatus(
        state=state,
        nozzles=dict(sorted(nozzles.items())),
        bed=temperatures.get("B"),
        progress=progress,
        file_name=file_name,
        file_size=file_size,
        elapsed=elapsed,
    )


def parse_firmware(text: str) -> str | None:
    """The FIRMWARE_NAME from an M115 reply ("TFT24"), or None if there is none."""
    for line in _lines(text):
        if (match := _FIRMWARE.search(line)) and match[1]:
            return match[1]
    return None
