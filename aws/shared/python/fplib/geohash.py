"""Geohash base32 estandar, sin dependencias externas."""
from __future__ import annotations

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def encode(lat: float, lon: float, precision: int = 6) -> str:
    lat_rng, lon_rng = [-90.0, 90.0], [-180.0, 180.0]
    out, bits, bit_count, even = [], 0, 0, True
    while len(out) < precision:
        rng, val = (lon_rng, lon) if even else (lat_rng, lat)
        mid = (rng[0] + rng[1]) / 2
        bits <<= 1
        if val >= mid:
            bits |= 1
            rng[0] = mid
        else:
            rng[1] = mid
        even = not even
        bit_count += 1
        if bit_count == 5:
            out.append(_BASE32[bits])
            bits, bit_count = 0, 0
    return "".join(out)
