"""Tests for the reply parser, on real captures from a Wanhao D12 (TFT24) and edge cases."""

from __future__ import annotations

from datetime import timedelta

import pytest

from mkswifi import PrinterState, PrinterStatus, Temperature, parse_firmware, parse_status
from mkswifi.parser import replied_to

# The five queries in one write, read for 4 s. M105's reply comes last, after "ok\n".
CAPTURE_BATCH = (
    "ok\r\nM997 PRINTING\r\nok\r\nM27 0\r\nok\r\nM994 1:/stacked.gcode;2013404224\r\n"
    "ok\r\nM992 00:18:28\r\nok\nT:200 /200 B:59 /60 T0:200 /200 T1:200 /0 @:0 B@:0\r\n"
)
# One query at a time; an unsolicited temperature line came before M994's reply.
CAPTURE_M105 = "ok\r\nT:200 /200 B:59 /60 T0:200 /200 T1:200 /0 @:0 B@:0"
CAPTURE_M994 = (
    "T:199 /200 B:60 /60 T0:199 /200 T1:200 /0 @:0 B@:0\r\nok\r\nM994 1:/stacked.gcode;2013404224"
)
CAPTURE_M115 = "ok\r\nFIRMWARE_NAME:TFT24\r\nT:200 /200 B:60 /60 T0:200 /200 T1:200 /0 @:0 B@:0\r\n"


def test_batch_capture() -> None:
    assert parse_status(CAPTURE_BATCH) == PrinterStatus(
        state=PrinterState.PRINTING,
        nozzles={0: Temperature(200, 200), 1: Temperature(200, 0)},
        bed=Temperature(59, 60),
        progress=0,
        file_name="stacked.gcode",
        file_size=2013404224,
        elapsed=timedelta(minutes=18, seconds=28),
    )
    assert replied_to(CAPTURE_BATCH) == {"M105", "M997", "M27", "M994", "M992"}


def test_single_m105_capture() -> None:
    status = parse_status(CAPTURE_M105)
    assert status.nozzle == Temperature(200, 200)
    assert status.bed == Temperature(59, 60)
    assert status.state is PrinterState.UNKNOWN
    assert status.progress is None
    assert status.file_name is None
    assert status.elapsed is None


def test_single_m994_capture_with_unsolicited_temperatures() -> None:
    status = parse_status(CAPTURE_M994)
    assert status.file_name == "stacked.gcode"
    assert status.file_size == 2013404224
    assert status.nozzle == Temperature(199, 200)
    assert replied_to(CAPTURE_M994) == {"M105", "M994"}


def test_order_does_not_matter() -> None:
    lines = CAPTURE_BATCH.replace("\r\n", "\n").splitlines()
    assert parse_status("\r\n".join(reversed(lines))) == parse_status(CAPTURE_BATCH)


def test_empty() -> None:
    assert parse_status("") == PrinterStatus()
    assert parse_status("ok\r\nok\n\r\n  \ngarbage\r\n") == PrinterStatus()
    assert replied_to("") == frozenset()


def test_last_temperature_line_wins() -> None:
    status = parse_status("T:20 /0 B:21 /0\r\nM997 IDLE\r\nT:150.5 /200.0 B:55.25 /60\r\n")
    assert status.nozzle == Temperature(150.5, 200.0)
    assert status.bed == Temperature(55.25, 60)


def test_ok_prefix_on_the_same_line() -> None:
    status = parse_status("ok T:200 /210 B:60 /60 @:0 B@:0\nok M997 IDLE\nok ok M27 42\n")
    assert status.nozzles == {0: Temperature(200, 210)}
    assert status.state is PrinterState.IDLE
    assert status.progress == 42


def test_nozzle_falls_back_to_t_without_t0() -> None:
    assert parse_status("T:180 /200 B:60 /60").nozzles == {0: Temperature(180, 200)}


def test_t0_takes_precedence_over_t() -> None:
    # On a dual-nozzle printer "T:" is the active nozzle, which may be T1.
    status = parse_status("T:210 /210 B:60 /60 T0:150 /0 T1:210 /210")
    assert status.nozzle == Temperature(150, 0)
    assert status.nozzles == {0: Temperature(150, 0), 1: Temperature(210, 210)}


def test_nozzles_are_sorted_and_keep_t_out_when_t0_missing_but_others_present() -> None:
    status = parse_status("T1:30 /0 T:25 /0 B:20 /0")
    assert list(status.nozzles) == [0, 1]
    assert status.nozzle == Temperature(25, 0)


def test_bed_power_is_not_the_bed() -> None:
    status = parse_status("T:200 /200 @:127 B@:64")
    assert status.bed is None


def test_negative_temperature() -> None:
    # A disconnected thermistor can read below zero.
    assert parse_status("T:-14.5 /0 B:20 /0").nozzle == Temperature(-14.5, 0)


def test_bed_only() -> None:
    status = parse_status("B:60 /60\r\n")
    assert status.bed == Temperature(60, 60)
    assert status.nozzles == {}
    assert status.nozzle is None


@pytest.mark.parametrize(
    ("reply", "state"),
    [
        ("M997 IDLE", PrinterState.IDLE),
        ("M997 PRINTING", PrinterState.PRINTING),
        ("M997 PAUSE", PrinterState.PAUSED),
        ("M997 PAUSED", PrinterState.PAUSED),
        ("m997 printing", PrinterState.PRINTING),
        ("M997 HEATING", PrinterState.UNKNOWN),
        ("M997", PrinterState.UNKNOWN),
    ],
)
def test_state(reply: str, state: PrinterState) -> None:
    assert parse_status(f"ok\r\n{reply}\r\n").state is state
    assert "M997" in replied_to(reply)


@pytest.mark.parametrize(
    ("reply", "progress"),
    [("M27 0", 0), ("M27 57", 57), ("M27 100", 100), ("M27", None), ("M27 ?", None)],
)
def test_progress(reply: str, progress: int | None) -> None:
    assert parse_status(reply).progress == progress
    assert "M27" in replied_to(reply)


@pytest.mark.parametrize(
    ("reply", "name", "size"),
    [
        ("M994 1:/stacked.gcode;2013404224", "stacked.gcode", 2013404224),
        ("M994 0:/benchy.gcode;1234", "benchy.gcode", 1234),
        ("M994 1:/models/part;v2.gcode;99", "models/part;v2.gcode", 99),
        ("M994 0:benchy.gcode;5", "benchy.gcode", 5),
        ("M994 benchy.gcode;5", "benchy.gcode", 5),
        ("M994 1:/benchy.gcode", "benchy.gcode", None),
        ("M994 1:/benchy.gcode;", "benchy.gcode", None),
        ("M994 1:/benchy.gcode;abc", "benchy.gcode", None),
        ("M994 1:/;0", None, 0),
        ("M994 ", None, None),
        ("M994", None, None),
    ],
)
def test_file(reply: str, name: str | None, size: int | None) -> None:
    status = parse_status(reply)
    assert status.file_name == name
    assert status.file_size == size
    assert "M994" in replied_to(reply)


@pytest.mark.parametrize(
    ("reply", "elapsed"),
    [
        ("M992 00:18:28", timedelta(minutes=18, seconds=28)),
        ("M992 01:02:03", timedelta(hours=1, minutes=2, seconds=3)),
        ("M992 123:59:59", timedelta(hours=123, minutes=59, seconds=59)),
        ("M992 0:0:0", timedelta()),
        ("M992", None),
        ("M992 --:--:--", None),
    ],
)
def test_elapsed(reply: str, elapsed: timedelta | None) -> None:
    assert parse_status(reply).elapsed == elapsed
    assert "M992" in replied_to(reply)


def test_crlf_lf_and_cr_line_endings() -> None:
    text = "M997 IDLE\rM27 5\nM992 00:00:07\r\n"
    status = parse_status(text)
    assert status.state is PrinterState.IDLE
    assert status.progress == 5
    assert status.elapsed == timedelta(seconds=7)


def test_command_names_need_a_boundary() -> None:
    assert parse_status("M9970 IDLE\r\nM270 5\r\n") == PrinterStatus()


@pytest.mark.parametrize(
    ("text", "firmware"),
    [
        (CAPTURE_M115, "TFT24"),
        ("FIRMWARE_NAME:TFT24", "TFT24"),
        ("ok FIRMWARE_NAME: TFT35 ", "TFT35"),
        (
            "FIRMWARE_NAME:Marlin 2.0.9.3 (Sep 1 2022) SOURCE_CODE_URL:https://github.com/x "
            "PROTOCOL_VERSION:1.0 MACHINE_TYPE:D12 EXTRUDER_COUNT:1",
            "Marlin 2.0.9.3 (Sep 1 2022)",
        ),
        ("FIRMWARE_NAME:\r\nok\r\n", None),
        ("ok\r\nT:200 /200\r\n", None),
        ("", None),
    ],
)
def test_parse_firmware(text: str, firmware: str | None) -> None:
    assert parse_firmware(text) == firmware


def test_firmware_is_tracked_as_a_reply() -> None:
    assert replied_to(CAPTURE_M115) == {"M115", "M105"}
