"""Keeps the C dissector for Wireshark's source tree in step with the catalog.

``wireshark/packet-celestron-aux.c`` is a dissector written for Wireshark itself
(epan/dissectors). Its payload decoders are hand-written C, ports of
``decoding.py`` with the same names, but its tables (device and command names,
mount models, and which decoder each command uses) are generated from the
built-in catalog into two marked blocks. Run::

    python -m celestron_aux.wireshark_c

to rewrite those blocks after changing the catalog or adding a decoder. A test
fails when the file is out of date or a decoder has no C port.

Only built-in names go into this file: it is meant for everyone, not just for
one person's catalog.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from . import catalog, decoding

C_FILE = Path(__file__).resolve().parent.parent / "wireshark" / "packet-celestron-aux.c"
_BLOCKS = ("TABLES", "DISPATCH")


def _c_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _value_string(name: str, items: Iterable[Tuple[int, str]], width: int = 2) -> str:
    rows = "".join(f"    {{ 0x{key:0{width}x}, {_c_string(value)} }},\n" for key, value in items)
    return f"static const value_string {name}[] = {{\n{rows}    {{ 0, NULL }}\n}};\n"


def _switch_set(function: str, addresses: Iterable[int]) -> str:
    cases = "".join(f"    case 0x{a:02x}:\n" for a in sorted(addresses))
    return (f"static bool\n{function}(uint8_t address)\n{{\n    switch (address) {{\n{cases}"
            "        return true;\n    default:\n        return false;\n    }\n}\n")


def _builtin_commands(device: int) -> List[Tuple[int, str]]:
    names = {**catalog.GENERIC_COMMANDS, **catalog._DEVICE_COMMANDS.get(device, {})}
    return sorted(names.items())


def tables_block() -> str:
    parts = [
        _value_string("celestron_aux_devices", sorted(catalog.DEVICE_NAMES.items())),
        "\n",
        _value_string("celestron_aux_generic_commands", sorted(catalog.GENERIC_COMMANDS.items())),
    ]
    # Devices with the same command table (the two motors) share one array.
    shared: Dict[Tuple[Tuple[int, str], ...], List[int]] = {}
    for device in sorted(catalog._DEVICE_COMMANDS):
        shared.setdefault(tuple(_builtin_commands(device)), []).append(device)
    switch = ""
    for commands, devices in shared.items():
        name = f"celestron_aux_commands_{devices[0]:02x}"
        parts += ["\n", _value_string(name, commands)]
        switch += "".join(f"    case 0x{d:02x}:\n" for d in devices) + f"        return {name};\n"
    parts += [
        "\n/* The command names of a device, or NULL when only the generic ones are known. */\n",
        "static const value_string *\ncelestron_aux_command_table(uint8_t device)\n{\n"
        f"    switch (device) {{\n{switch}    default:\n        return NULL;\n    }}\n}}\n",
        "\n", _switch_set("celestron_aux_is_controller", catalog.CONTROLLERS),
        "\n", _switch_set("celestron_aux_is_motor_client", catalog.MOTOR_CLIENTS),
        "\n", _switch_set("celestron_aux_is_motor", catalog.MOTORS),
        f"\n#define CELESTRON_AUX_SCANNER 0x{catalog.Addr.SCANNER:02x}\n\n",
        _value_string("celestron_aux_mount_models", sorted(catalog.MOUNT_MODELS.items()), width=4),
        "\n",
        _value_string("celestron_aux_mount_families", sorted(catalog.MOUNT_FAMILIES.items())),
    ]
    return "".join(parts)


def _c_name(python_name: str) -> str:
    return "decode" + python_name  # _motor_position -> decode_motor_position


def dispatch_block() -> str:
    by_device: Dict[Optional[int], List[Tuple[int, str]]] = {}
    for (device, command), decoder in decoding._DECODERS.items():
        by_device.setdefault(device, []).append((command, _c_name(decoder.__name__)))

    def command_switch(items: List[Tuple[int, str]], indent: str) -> str:
        return "".join(f"{indent}case 0x{c:02x}: return {fn};\n" for c, fn in sorted(items))

    devices = ""
    for device in sorted(d for d in by_device if d is not None):
        devices += (f"    case 0x{device:02x}:\n        switch (command) {{\n"
                    f"{command_switch(by_device[device], '        ')}"
                    "        default: break;\n        }\n        break;\n")
    any_device = command_switch(by_device.get(None, []), "    ")
    return (
        "/* The decoder of a device's command, as decoding.py registers them: the device's\n"
        " * own decoder first, then one that applies to every device. */\n"
        "static celestron_aux_decoder_t\ncelestron_aux_decoder(uint8_t device, uint8_t command)\n{\n"
        f"    switch (device) {{\n{devices}    default:\n        break;\n    }}\n"
        f"    switch (command) {{\n{any_device}    default:\n        return NULL;\n    }}\n}}\n"
    )


def _replace_block(source: str, name: str, body: str) -> str:
    begin = f"/* BEGIN GENERATED {name}: python -m celestron_aux.wireshark_c */\n"
    end = f"/* END GENERATED {name} */"
    start, stop = source.index(begin) + len(begin), source.index(end)
    return source[:start] + body + source[stop:]


def generated_source(source: str) -> str:
    """``source`` with both generated blocks rewritten."""
    source = _replace_block(source, "TABLES", tables_block())
    return _replace_block(source, "DISPATCH", dispatch_block())


def c_decoder_names(source: str) -> List[str]:
    """Python names of the decoders that have a C port (``decode_x`` defines ``_x``)."""
    return [m[len("decode"):] for m in re.findall(r"^(decode_\w+)\(", source, flags=re.MULTILINE)]


def unported_decoders(source: str) -> List[str]:
    ported = set(c_decoder_names(source))
    return sorted({fn.__name__ for fn in decoding._DECODERS.values()} - ported)


def main(argv: Optional[List[str]] = None) -> int:
    path = Path(argv[0]) if argv else C_FILE
    source = path.read_text(encoding="utf-8")
    updated = generated_source(source)
    missing = unported_decoders(updated)
    if updated != source:
        path.write_text(updated, encoding="utf-8")
        print(f"Updated {path}.")
    else:
        print(f"{path} is up to date.")
    if missing:
        print("No C port yet (add decode" + ", decode".join(missing) + "): their payloads stay hex.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
