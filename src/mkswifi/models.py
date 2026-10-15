"""Data returned by the printer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum


class PrinterState(StrEnum):
    """What M997 reports."""

    IDLE = "idle"
    PRINTING = "printing"
    PAUSED = "paused"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Temperature:
    """A heater's current and target temperature, in °C."""

    current: float
    target: float


@dataclass(frozen=True, slots=True)
class PrinterStatus:
    """One snapshot of the printer.

    Fields stay None when the printer did not answer that query. Some firmwares
    (the TFT24 on a Wanhao D12, for one) always report a progress of 0.
    """

    state: PrinterState = PrinterState.UNKNOWN
    nozzles: dict[int, Temperature] = field(default_factory=dict)
    bed: Temperature | None = None
    progress: int | None = None
    file_name: str | None = None
    file_size: int | None = None
    elapsed: timedelta | None = None

    @property
    def nozzle(self) -> Temperature | None:
        """The first nozzle, which is the only one on most printers."""
        return self.nozzles.get(0)
