"""Talk to the MKS WIFI module over raw TCP."""

from __future__ import annotations

import asyncio
import contextlib

from .exceptions import MksWifiConnectionError
from .models import PrinterStatus
from .parser import parse_firmware, parse_status, replied_to

# Read-only queries: temperatures, state, progress, current file, elapsed time.
_STATUS_QUERIES = ("M105", "M997", "M27", "M994", "M992")
_FIRMWARE_QUERIES = ("M115",)
_CHUNK_SIZE = 4096
_CLOSE_TIMEOUT = 1.0


def _complete_lines(data: bytearray) -> str:
    """The text up to the last line terminator, so a half-received line is not trusted yet."""
    end = max(data.rfind(b"\n"), data.rfind(b"\r")) + 1
    return data[:end].decode("utf-8", errors="replace")


class MksWifiClient:
    """Client for one printer.

    Each call opens its own connection and closes it straight away: the module
    only accepts a handful of clients, and the slicer may already hold one.
    """

    def __init__(self, host: str, port: int = 8080, timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    async def get_status(self) -> PrinterStatus:
        """Query temperatures, state, progress, file and elapsed time.

        Raises MksWifiConnectionError if the printer cannot be reached or sends
        nothing in time. If only some replies arrive in time, the others stay None.
        """
        return parse_status(await self._query(_STATUS_QUERIES))

    async def get_firmware(self) -> str | None:
        """The firmware name from M115 ("TFT24"), or None if the reply has none."""
        return parse_firmware(await self._query(_FIRMWARE_QUERIES))

    async def _query(self, commands: tuple[str, ...]) -> str:
        """Send commands in one write, then read until each one is answered or time runs out."""
        expected = frozenset(commands)
        received = bytearray()
        # One deadline for connecting and reading together.
        deadline = asyncio.get_running_loop().time() + self.timeout
        try:
            async with asyncio.timeout_at(deadline):
                reader, writer = await asyncio.open_connection(self.host, self.port)
        except (OSError, TimeoutError) as err:
            raise MksWifiConnectionError(
                f"Cannot connect to {self.host}:{self.port}: {err!r}"
            ) from err
        try:
            async with asyncio.timeout_at(deadline):
                writer.write("".join(f"{command}\r\n" for command in commands).encode("ascii"))
                await writer.drain()
                while not expected <= replied_to(_complete_lines(received)):
                    chunk = await reader.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    received += chunk
        except (OSError, TimeoutError) as err:
            # Keep whatever arrived before the timeout or the reset: a partial answer.
            if not received:
                raise MksWifiConnectionError(
                    f"No reply from {self.host}:{self.port}: {err!r}"
                ) from err
        finally:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                async with asyncio.timeout(_CLOSE_TIMEOUT):
                    await writer.wait_closed()
        if not received:
            # The module accepted, then hung up: usually it has too many clients.
            raise MksWifiConnectionError(f"{self.host}:{self.port} closed without replying")
        return received.decode("utf-8", errors="replace")
