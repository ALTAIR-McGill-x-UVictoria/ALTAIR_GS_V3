"""
Telescope tracking math.

Converts flight payload GPS position to azimuth/elevation angles
relative to the ground station, and optionally to equatorial RA/Dec
for mounts that require it (e.g. ZWO AM5 via ASCOM).

Ground station location is hardcoded pending COM port GPS integration.
# TODO: replace GS_LAT/GS_LON/GS_ALT with live GPS feed from serial controller
"""
from __future__ import annotations

import math
import time as _time

# ---------------------------------------------------------------------------
# Ground station fixed position
# TODO: replace with COM port GPS feed
# ---------------------------------------------------------------------------
GS_LAT: float =  45.5088   # deg N
GS_LON: float = -73.5542   # deg E (negative = West)
GS_ALT: float =   0.0      # m MSL

_DEG2RAD = math.pi / 180.0
_RAD2DEG = 180.0 / math.pi


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Great-circle distance between two points on Earth (WGS-84 mean radius).

    Parameters are in decimal degrees.
    Returns distance in metres.
    """
    R = 6_371_000.0  # Earth mean radius, metres
    phi1 = lat1 * _DEG2RAD
    phi2 = lat2 * _DEG2RAD
    dphi = (lat2 - lat1) * _DEG2RAD
    dlam = (lon2 - lon1) * _DEG2RAD

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2.0 * R * math.asin(math.sqrt(a))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Initial bearing from point 1 to point 2 (forward azimuth).

    Parameters are in decimal degrees.
    Returns bearing in degrees [0, 360).
    """
    phi1 = lat1 * _DEG2RAD
    phi2 = lat2 * _DEG2RAD
    dlam = (lon2 - lon1) * _DEG2RAD

    x = math.sin(dlam) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    az = math.atan2(x, y) * _RAD2DEG
    return az % 360.0


def elevation(ground_alt: float, target_alt: float, slant_range_m: float) -> float:
    """
    Elevation angle from the ground station to the target.

    ground_alt   : ground station altitude, metres MSL
    target_alt   : target altitude, metres MSL
    slant_range_m: horizontal great-circle distance, metres

    Returns elevation in degrees [-90, 90].
    Clamps to -90/+90 to avoid domain errors when range ~= 0.
    """
    if slant_range_m < 1.0:
        # Directly overhead (or same point) — return 90°
        return 90.0
    alt_diff = target_alt - ground_alt
    return math.atan2(alt_diff, slant_range_m) * _RAD2DEG


def _julian_date(unix_utc: float) -> float:
    """Convert a Unix UTC timestamp to Julian Date."""
    return unix_utc / 86400.0 + 2440587.5


def _gmst_deg(jd: float) -> float:
    """
    Greenwich Mean Sidereal Time in degrees for a given Julian Date.
    Uses the IAU 1982 formula (accurate to ~0.1 s over the next century).
    """
    T = (jd - 2451545.0) / 36525.0  # Julian centuries from J2000.0
    theta = (280.46061837
              + 360.98564736629 * (jd - 2451545.0)
              + 0.000387933 * T ** 2
              - T ** 3 / 38_710_000.0)
    return theta % 360.0


def azalt_to_radec(
    azimuth: float,
    elevation: float,
    observer_lat: float = GS_LAT,
    unix_utc: float | None = None,
) -> tuple[float, float]:
    """
    Convert topocentric Az/El to equatorial RA/Dec.

    Parameters
    ----------
    azimuth      : degrees, 0 = North, 90 = East
    elevation    : degrees, above horizon
    observer_lat : observer geodetic latitude, degrees
    unix_utc     : UTC time as Unix timestamp; defaults to now

    Returns
    -------
    (ra_hours, dec_deg)  —  RA in decimal hours [0, 24), Dec in degrees [-90, 90]
    """
    if unix_utc is None:
        unix_utc = _time.time()

    lat_r = observer_lat * _DEG2RAD
    az_r  = azimuth      * _DEG2RAD
    el_r  = elevation    * _DEG2RAD

    # Hour angle and declination from Az/El
    sin_dec = (math.sin(el_r) * math.sin(lat_r)
               + math.cos(el_r) * math.cos(lat_r) * math.cos(az_r))
    dec_r  = math.asin(max(-1.0, min(1.0, sin_dec)))

    cos_ha_num = math.sin(el_r) - math.sin(lat_r) * sin_dec
    cos_ha_den = math.cos(lat_r) * math.cos(dec_r)
    if abs(cos_ha_den) < 1e-9:
        ha_r = 0.0
    else:
        cos_ha = cos_ha_num / cos_ha_den
        ha_r   = math.acos(max(-1.0, min(1.0, cos_ha)))
        # Azimuth > 180° means target is west of meridian → HA positive
        if math.sin(az_r) > 0:
            ha_r = -ha_r  # east of meridian → negative HA

    ha_deg = ha_r * _RAD2DEG

    # Local Sidereal Time → RA = LST - HA
    jd  = _julian_date(unix_utc)
    lst = (_gmst_deg(jd) + GS_LON) % 360.0   # add east longitude
    ra_deg = (lst - ha_deg) % 360.0
    ra_hours = ra_deg / 15.0

    return ra_hours, dec_r * _RAD2DEG


def calculate_tracking_params(
    payload_lat: float,
    payload_lon: float,
    payload_alt_m: float,
) -> dict:
    """
    Calculate all tracking parameters needed to point the telescope.

    Parameters
    ----------
    payload_lat   : payload latitude, decimal degrees
    payload_lon   : payload longitude, decimal degrees
    payload_alt_m : payload altitude, metres MSL

    Returns
    -------
    dict with keys:
        azimuth     (deg, 0-360 clockwise from North)
        elevation   (deg, above horizon)
        distance_m  (horizontal great-circle distance, metres)
        slant_m     (3-D slant range, metres)
        gs_lat      (ground station latitude used)
        gs_lon      (ground station longitude used)
        gs_alt      (ground station altitude used)
    """
    dist_m = haversine_distance(GS_LAT, GS_LON, payload_lat, payload_lon)
    az     = bearing(GS_LAT, GS_LON, payload_lat, payload_lon)
    el     = elevation(GS_ALT, payload_alt_m, dist_m)

    alt_diff = payload_alt_m - GS_ALT
    slant_m  = math.sqrt(dist_m ** 2 + alt_diff ** 2)

    ra_h, dec_d = azalt_to_radec(az, el)

    return {
        "azimuth":    round(az,     4),
        "elevation":  round(el,     4),
        "ra_hours":   round(ra_h,   6),
        "dec_deg":    round(dec_d,  6),
        "distance_m": round(dist_m, 1),
        "slant_m":    round(slant_m, 1),
        "gs_lat":     GS_LAT,
        "gs_lon":     GS_LON,
        "gs_alt":     GS_ALT,
    }
