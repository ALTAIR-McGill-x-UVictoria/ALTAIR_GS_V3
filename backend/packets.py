"""
Packet definitions mirroring altairfc/telemetry/packets/.
Update field lists here when the flight computer side changes.

Wire format (little-endian):
  [SYNC:1][PKT_ID:1][SEQ:1][TIMESTAMP:8 float64][LEN:2 uint16][DATA:N][CRC16:2]
"""
from __future__ import annotations

import binascii
import struct
from dataclasses import dataclass, field
from typing import Any, ClassVar

SYNC_BYTE   = 0xAA
_HEADER     = struct.Struct("<BBBdH")   # sync, pkt_id, seq, timestamp, length
_CRC        = struct.Struct("<H")
HEADER_SIZE = _HEADER.size              # 13
CRC_SIZE    = _CRC.size                 # 2
MIN_FRAME   = HEADER_SIZE + CRC_SIZE    # 15


# ---------------------------------------------------------------------------
# Packet definitions
# ---------------------------------------------------------------------------

@dataclass
class AttitudePacket:
    PACKET_ID:   ClassVar[int]          = 0x01
    LABEL:       ClassVar[str]          = "Attitude"
    STRUCT_FMT:  ClassVar[struct.Struct] = struct.Struct("<ffffff")
    FIELDS:      ClassVar[list[dict]]   = [
        {"name": "roll",       "label": "Roll",        "unit": "rad"},
        {"name": "pitch",      "label": "Pitch",       "unit": "rad"},
        {"name": "yaw",        "label": "Yaw",         "unit": "rad"},
        {"name": "rollspeed",  "label": "Roll Rate",   "unit": "rad/s"},
        {"name": "pitchspeed", "label": "Pitch Rate",  "unit": "rad/s"},
        {"name": "yawspeed",   "label": "Yaw Rate",    "unit": "rad/s"},
    ]
    roll:       float = 0.0
    pitch:      float = 0.0
    yaw:        float = 0.0
    rollspeed:  float = 0.0
    pitchspeed: float = 0.0
    yawspeed:   float = 0.0


@dataclass
class PowerPacket:
    PACKET_ID:   ClassVar[int]          = 0x02
    LABEL:       ClassVar[str]          = "Power"
    STRUCT_FMT:  ClassVar[struct.Struct] = struct.Struct("<fff")
    FIELDS:      ClassVar[list[dict]]   = [
        {"name": "voltage_bus",   "label": "Bus Voltage",    "unit": "V"},
        {"name": "current_total", "label": "Total Current",  "unit": "A"},
        {"name": "temperature",   "label": "Temperature",    "unit": "°C"},
    ]
    voltage_bus:   float = 0.0
    current_total: float = 0.0
    temperature:   float = 0.0


@dataclass
class VescPacket:
    PACKET_ID:   ClassVar[int]          = 0x03
    LABEL:       ClassVar[str]          = "VESC"
    STRUCT_FMT:  ClassVar[struct.Struct] = struct.Struct("<fffff")
    FIELDS:      ClassVar[list[dict]]   = [
        {"name": "rpm",             "label": "RPM",              "unit": "rpm"},
        {"name": "duty_cycle",      "label": "Duty Cycle",       "unit": "%"},
        {"name": "motor_current",   "label": "Motor Current",    "unit": "A"},
        {"name": "input_voltage",   "label": "Input Voltage",    "unit": "V"},
        {"name": "temperature_mos", "label": "MOSFET Temp",      "unit": "°C"},
    ]
    rpm:             float = 0.0
    duty_cycle:      float = 0.0
    motor_current:   float = 0.0
    input_voltage:   float = 0.0
    temperature_mos: float = 0.0


@dataclass
class PhotodiodePacket:
    PACKET_ID:   ClassVar[int]          = 0x04
    LABEL:       ClassVar[str]          = "Photodiode"
    STRUCT_FMT:  ClassVar[struct.Struct] = struct.Struct("<ffff")
    FIELDS:      ClassVar[list[dict]]   = [
        {"name": "channel_0", "label": "Channel 0", "unit": "V"},
        {"name": "channel_1", "label": "Channel 1", "unit": "V"},
        {"name": "channel_2", "label": "Channel 2", "unit": "V"},
        {"name": "channel_3", "label": "Channel 3", "unit": "V"},
    ]
    channel_0: float = 0.0
    channel_1: float = 0.0
    channel_2: float = 0.0
    channel_3: float = 0.0


@dataclass
class GpsPacket:
    """Packet ID 0x05 — Fused GPS from Pixhawk GLOBAL_POSITION_INT."""
    PACKET_ID:   ClassVar[int]          = 0x05
    LABEL:       ClassVar[str]          = "GPS"
    STRUCT_FMT:  ClassVar[struct.Struct] = struct.Struct("<fffff")
    FIELDS:      ClassVar[list[dict]]   = [
        {"name": "lat",          "label": "Latitude",     "unit": "deg"},
        {"name": "lon",          "label": "Longitude",    "unit": "deg"},
        {"name": "alt",          "label": "Altitude MSL", "unit": "m"},
        {"name": "relative_alt", "label": "Altitude AGL", "unit": "m"},
        {"name": "hdg",          "label": "Heading",      "unit": "deg"},
    ]
    lat:          float = 0.0
    lon:          float = 0.0
    alt:          float = 0.0
    relative_alt: float = 0.0
    hdg:          float = 0.0


REGISTRY: dict[int, type] = {
    cls.PACKET_ID: cls
    for cls in (AttitudePacket, PowerPacket, VescPacket, PhotodiodePacket, GpsPacket)
}


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------

def decode_frame(raw: bytes) -> dict[str, Any] | None:
    """
    Decode one complete frame. Returns a JSON-serialisable dict on success,
    None on any validation failure.

    Returned dict shape:
    {
      "packet_id": 1,
      "label":     "Attitude",
      "seq":       42,
      "timestamp": 12.345,
      "fields": [
        {"name": "roll", "label": "Roll", "unit": "rad", "value": -0.012},
        ...
      ]
    }
    """
    if len(raw) < MIN_FRAME:
        return None

    sync, pkt_id, seq, timestamp, length = _HEADER.unpack_from(raw, 0)

    if sync != SYNC_BYTE:
        return None
    if len(raw) < HEADER_SIZE + length + CRC_SIZE:
        return None

    received_crc = _CRC.unpack_from(raw, HEADER_SIZE + length)[0]
    computed_crc = binascii.crc_hqx(raw[1: HEADER_SIZE + length], 0xFFFF)
    if received_crc != computed_crc:
        return None

    cls = REGISTRY.get(pkt_id)
    if cls is None:
        return None

    payload = raw[HEADER_SIZE: HEADER_SIZE + length]
    if len(payload) != cls.STRUCT_FMT.size:
        return None

    values = cls.STRUCT_FMT.unpack(payload)
    field_defs = cls.FIELDS

    return {
        "packet_id": pkt_id,
        "label":     cls.LABEL,
        "seq":       seq,
        "timestamp": round(timestamp, 4),
        "fields": [
            {**fd, "value": round(v, 6)}
            for fd, v in zip(field_defs, values)
        ],
    }
