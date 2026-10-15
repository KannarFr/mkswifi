# mkswifi

Asyncio client for 3D printers fitted with a Makerbase **MKS WIFI** module (MKS Robin
boards). Pure standard library, fully typed, built to back a Home Assistant integration.

Known to work with the **Wanhao D12** (firmware `TFT24`). Other MKS Robin printers that
use the same module (Two Trees, Flying Bear, ...) should work too.

## Install

```sh
pip install mkswifi
```

Python 3.12 or newer, no dependencies.

## Usage

```python
import asyncio

from mkswifi import MksWifiClient, MksWifiConnectionError


async def main() -> None:
    client = MksWifiClient("192.168.1.42")  # port 8080, timeout 5 s by default
    try:
        status = await client.get_status()
    except MksWifiConnectionError as err:
        print(f"printer unreachable: {err}")
        return
    print(status.state, status.nozzle, status.bed, status.file_name, status.elapsed)
    print(await client.get_firmware())  # "TFT24"


asyncio.run(main())
```

Each call opens a connection, sends its queries, reads the replies and closes. The module
accepts very few clients at once (a slicer such as Cura's MKS plugin may already hold
one), so nothing stays connected. `MksWifiConnectionError` is raised when the printer
cannot be reached or sends nothing before the timeout; when only some replies arrive, the
missing fields of `PrinterStatus` are `None`.

Only read-only queries are ever sent.

## Protocol

Raw TCP on port 8080, one G-code per line ending in `\r\n`. Each command is answered with
`ok` and a reply line, in any order; the printer also sends temperature lines on its own.

| Command | Reply                                                 | Field(s)                          |
| ------- | ----------------------------------------------------- | --------------------------------- |
| `M105`  | `T:200 /200 B:59 /60 T0:200 /200 T1:200 /0 @:0 B@:0`  | `nozzles`, `nozzle`, `bed`        |
| `M997`  | `M997 IDLE` / `M997 PRINTING` / `M997 PAUSE`          | `state`                           |
| `M27`   | `M27 0`                                               | `progress` (percent)              |
| `M994`  | `M994 1:/stacked.gcode;2013404224`                    | `file_name`, `file_size`          |
| `M992`  | `M992 00:18:28`                                       | `elapsed`                         |
| `M115`  | `FIRMWARE_NAME:TFT24`                                 | `get_firmware()`                  |

## Caveats

- **Progress is always 0** on the Wanhao D12's `TFT24` firmware, even mid-print: `M27`
  is answered but never updated. Use `state` and `elapsed` instead.
- The file size from `M994` can be nonsense (2 GB for a small file); it is passed on as is.
- Single-nozzle printers may still report a `T1` with a target of 0; nozzles are exposed
  as reported, and `status.nozzle` is nozzle 0.

## License

MIT
