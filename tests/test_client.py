"""Tests for the client against a fake MKS WIFI module listening on 127.0.0.1."""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest

from mkswifi import MksWifiClient, MksWifiConnectionError, PrinterState, Temperature

from .test_parser import CAPTURE_BATCH, CAPTURE_M115

STATUS_REQUEST = b"M105\r\nM997\r\nM27\r\nM994\r\nM992\r\n"
TEMPERATURES = b"T:200 /200 B:59 /60 T0:200 /200 T1:200 /0 @:0 B@:0\r\n"
REPLIES = {
    b"M105": b"ok\n" + TEMPERATURES,
    b"M997": b"ok\r\nM997 PRINTING\r\n",
    b"M27": b"ok\r\nM27 0\r\n",
    b"M994": b"ok\r\nM994 1:/stacked.gcode;2013404224\r\n",
    b"M992": b"ok\r\nM992 00:18:28\r\n",
    b"M115": b"ok\r\nFIRMWARE_NAME:TFT24\r\n",
}

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


class FakePrinter:
    """A TCP server that runs a handler per connection and records what it was sent."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.port = 0
        self._handler: Handler | None = None
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self, handler: Handler) -> int:
        self._handler = handler
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert self._handler is not None
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            await self._handler(reader, writer)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    async def read_request(self, reader: asyncio.StreamReader, size: int) -> bytes:
        data = await reader.readexactly(size)
        self.received.append(data)
        return data

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            await self._server.wait_closed()


@pytest.fixture
async def printer() -> AsyncIterator[FakePrinter]:
    fake = FakePrinter()
    yield fake
    await fake.stop()


async def hold(reader: asyncio.StreamReader) -> None:
    """Keep the connection open until the client hangs up, like the real module."""
    await reader.read()


def assert_full_status(status: Any) -> None:
    assert status.state is PrinterState.PRINTING
    assert status.nozzle == Temperature(200, 200)
    assert status.nozzles[1] == Temperature(200, 0)
    assert status.bed == Temperature(59, 60)
    assert status.progress == 0
    assert status.file_name == "stacked.gcode"
    assert status.file_size == 2013404224
    assert status.elapsed == timedelta(minutes=18, seconds=28)


async def test_in_order(printer: FakePrinter) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await printer.read_request(reader, len(STATUS_REQUEST))
        for command in request.split():
            writer.write(REPLIES[command])
        await writer.drain()
        await hold(reader)

    port = await printer.start(handler)
    started = time.monotonic()
    status = await MksWifiClient("127.0.0.1", port, timeout=5).get_status()
    # Returned as soon as every reply was in, not at the timeout.
    assert time.monotonic() - started < 1
    assert_full_status(status)
    # Exactly the five read-only queries, in one write.
    assert printer.received == [STATUS_REQUEST]


async def test_out_of_order_real_capture(printer: FakePrinter) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await printer.read_request(reader, len(STATUS_REQUEST))
        writer.write(CAPTURE_BATCH.encode())
        await hold(reader)

    port = await printer.start(handler)
    started = time.monotonic()
    assert_full_status(await MksWifiClient("127.0.0.1", port).get_status())
    assert time.monotonic() - started < 1


async def test_slow_and_split_mid_line(printer: FakePrinter) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await printer.read_request(reader, len(STATUS_REQUEST))
        # Everything a few bytes at a time.
        data = CAPTURE_BATCH.encode()
        for start in range(0, len(data), 7):
            writer.write(data[start : start + 7])
            await writer.drain()
            await asyncio.sleep(0.005)
        await hold(reader)

    port = await printer.start(handler)
    assert_full_status(await MksWifiClient("127.0.0.1", port, timeout=5).get_status())


async def test_unsolicited_temperatures_count_as_the_m105_reply(printer: FakePrinter) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await printer.read_request(reader, len(STATUS_REQUEST))
        writer.write(b"T:20 /0 B:21 /0\r\n" + CAPTURE_BATCH.encode().replace(TEMPERATURES, b""))
        await writer.drain()
        await asyncio.sleep(2)
        writer.write(TEMPERATURES)
        await hold(reader)

    port = await printer.start(handler)
    started = time.monotonic()
    status = await MksWifiClient("127.0.0.1", port, timeout=5).get_status()
    # The module sends temperatures on its own every few seconds: no need to wait for more.
    assert time.monotonic() - started < 1
    assert status.nozzle == Temperature(20, 0)
    assert status.bed == Temperature(21, 0)
    assert status.state is PrinterState.PRINTING


async def test_does_not_stop_on_a_half_received_line(printer: FakePrinter) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await printer.read_request(reader, len(STATUS_REQUEST))
        # All replies but the temperatures, then "T:2" for a while: must not be read as 2 °C.
        writer.write(CAPTURE_BATCH.encode().replace(TEMPERATURES, b"T:2"))
        await writer.drain()
        await asyncio.sleep(0.1)
        writer.write(TEMPERATURES[3:])
        await hold(reader)

    port = await printer.start(handler)
    assert_full_status(await MksWifiClient("127.0.0.1", port).get_status())


async def test_unterminated_last_line_is_used_at_the_timeout(printer: FakePrinter) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await printer.read_request(reader, len(STATUS_REQUEST))
        writer.write(CAPTURE_BATCH.encode().rstrip())
        await hold(reader)

    port = await printer.start(handler)
    started = time.monotonic()
    assert_full_status(await MksWifiClient("127.0.0.1", port, timeout=0.3).get_status())
    assert time.monotonic() - started >= 0.3


async def test_partial_replies_then_silence(printer: FakePrinter) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await printer.read_request(reader, len(STATUS_REQUEST))
        writer.write(REPLIES[b"M997"] + REPLIES[b"M992"])
        await hold(reader)

    port = await printer.start(handler)
    status = await MksWifiClient("127.0.0.1", port, timeout=0.3).get_status()
    assert status.state is PrinterState.PRINTING
    assert status.elapsed == timedelta(minutes=18, seconds=28)
    assert status.nozzle is None
    assert status.bed is None
    assert status.progress is None
    assert status.file_name is None


async def test_partial_replies_then_hang_up(printer: FakePrinter) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await printer.read_request(reader, len(STATUS_REQUEST))
        writer.write(REPLIES[b"M105"])
        await writer.drain()

    port = await printer.start(handler)
    started = time.monotonic()
    status = await MksWifiClient("127.0.0.1", port, timeout=5).get_status()
    assert time.monotonic() - started < 1
    assert status.nozzle == Temperature(200, 200)
    assert status.state is PrinterState.UNKNOWN


async def test_silent(printer: FakePrinter) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await hold(reader)

    port = await printer.start(handler)
    with pytest.raises(MksWifiConnectionError, match="No reply"):
        await MksWifiClient("127.0.0.1", port, timeout=0.2).get_status()


async def test_hangs_up_without_replying(printer: FakePrinter) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Too many clients: the module accepts, then closes.
        await printer.read_request(reader, len(STATUS_REQUEST))

    port = await printer.start(handler)
    with pytest.raises(MksWifiConnectionError, match="closed without replying"):
        await MksWifiClient("127.0.0.1", port, timeout=5).get_status()


async def test_refused() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # Nothing listens on that port any more.
    with pytest.raises(MksWifiConnectionError, match="Cannot connect") as info:
        await MksWifiClient("127.0.0.1", port).get_status()
    assert isinstance(info.value.__cause__, OSError)


async def test_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never_connects(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(10)

    monkeypatch.setattr(asyncio, "open_connection", never_connects)
    with pytest.raises(MksWifiConnectionError, match="Cannot connect") as info:
        await MksWifiClient("192.0.2.1", timeout=0.1).get_status()
    assert isinstance(info.value.__cause__, TimeoutError)


class _ResettingReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def read(self, n: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        raise ConnectionResetError("reset by peer")


class _Writer:
    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        raise BrokenPipeError


@pytest.mark.parametrize(
    ("chunks", "expected_state"),
    [([REPLIES[b"M997"]], PrinterState.PRINTING), ([], None)],
)
async def test_reset_while_reading(
    monkeypatch: pytest.MonkeyPatch, chunks: list[bytes], expected_state: PrinterState | None
) -> None:
    writer = _Writer()

    async def open_connection(*args: object, **kwargs: object) -> tuple[Any, Any]:
        return _ResettingReader(chunks), writer

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    client = MksWifiClient("printer.local")
    if expected_state is None:
        with pytest.raises(MksWifiConnectionError, match="No reply"):
            await client.get_status()
    else:
        assert (await client.get_status()).state is expected_state
    # The connection is closed either way, even when closing itself fails.
    assert writer.closed
    assert writer.written == STATUS_REQUEST


async def test_cancelled_closes_the_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _Writer()

    class _HangingReader:
        async def read(self, n: int) -> bytes:
            await asyncio.sleep(10)
            return b""

    async def open_connection(*args: object, **kwargs: object) -> tuple[Any, Any]:
        return _HangingReader(), writer

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    task = asyncio.create_task(MksWifiClient("printer.local").get_status())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.closed


async def test_get_firmware(printer: FakePrinter) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await printer.read_request(reader, len(b"M115\r\n"))
        writer.write(CAPTURE_M115.encode())
        await hold(reader)

    port = await printer.start(handler)
    started = time.monotonic()
    assert await MksWifiClient("127.0.0.1", port).get_firmware() == "TFT24"
    assert time.monotonic() - started < 1
    assert printer.received == [b"M115\r\n"]


async def test_get_firmware_without_a_name(printer: FakePrinter) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await printer.read_request(reader, len(b"M115\r\n"))
        writer.write(b"ok\r\n" + TEMPERATURES)
        await hold(reader)

    port = await printer.start(handler)
    assert await MksWifiClient("127.0.0.1", port, timeout=0.2).get_firmware() is None


async def test_get_firmware_unreachable() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(MksWifiConnectionError):
        await MksWifiClient("127.0.0.1", port).get_firmware()


def test_defaults() -> None:
    client = MksWifiClient("printer.local")
    assert (client.host, client.port, client.timeout) == ("printer.local", 8080, 5.0)
