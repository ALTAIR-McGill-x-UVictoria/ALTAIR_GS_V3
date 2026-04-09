"""
GS-side command frame builder.

Imports the FC command package via the same sys.path injection used by
backend/packets.py, ensuring a single source of truth for command definitions.

Usage:
    from backend.commands import build_command_frame
    from telemetry.commands.arm import ArmCommandPacket

    frame = build_command_frame(ArmCommandPacket(arm_state=1))
    serial_reader.send_command(frame)
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: reuse the FC root path already injected by backend/packets.py.
# If packets.py hasn't been imported yet, inject it ourselves.
# ---------------------------------------------------------------------------

_FC_ROOT = (Path(__file__).parent.parent.parent / "Altairfc_V2" / "altairfc").resolve()

if not _FC_ROOT.exists():
    raise FileNotFoundError(
        f"Flight computer source not found at {_FC_ROOT}.\n"
        "Ensure Altairfc_V2 is checked out next to ALTAIR_GS_V3."
    )

if str(_FC_ROOT) not in sys.path:
    sys.path.insert(0, str(_FC_ROOT))

# Auto-discover command modules so their @register decorators fire
_COMMANDS_DIR = _FC_ROOT / "telemetry" / "commands"
for _p in sorted(_COMMANDS_DIR.glob("*.py")):
    if _p.stem not in ("__init__",):
        importlib.import_module(f"telemetry.commands.{_p.stem}")

from telemetry.command_registry import command_registry  # noqa: E402
from telemetry.serializer import PacketSerializer         # noqa: E402

_serializer = PacketSerializer()
_seq_counters: dict[int, int] = {}


def build_command_frame(cmd_packet: object) -> bytes:
    """
    Pack a command dataclass into a wire frame.

    Uses command_registry (not packet_registry) so command IDs are looked up
    from the correct namespace.
    """
    cmd_id = command_registry.get_id(type(cmd_packet))
    if cmd_id is None:
        raise ValueError(
            f"Command type {type(cmd_packet).__name__} is not registered in command_registry"
        )
    seq = _seq_counters.get(cmd_id, 0)
    _seq_counters[cmd_id] = (seq + 1) & 0xFF
    return _serializer.pack(cmd_packet, seq=seq, registry=command_registry)
