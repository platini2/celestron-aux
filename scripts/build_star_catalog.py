#!/usr/bin/env python3
# celestron-aux · Copyright (C) 2021-2026 Patricio Latini · SPDX-License-Identifier: GPL-3.0-or-later
"""Build celestron_aux/data/hipparcos.bin.gz, the stars the StarSense emulator photographs.

Input: the Hipparcos main catalogue (ESA 1997) as NASA HEASARC publishes it,
heasarc_hipparcos.tdat.gz from
https://heasarc.gsfc.nasa.gov/FTP/heasarc/dbase/tdat_files/heasarc_hipparcos.tdat.gz

    python3 scripts/build_star_catalog.py path/to/heasarc_hipparcos.tdat.gz

Keeps stars to V magnitude 9, moves them from the catalogue epoch (J1991.25)
to J2000 with their proper motions, and writes them sorted by declination as
little-endian records: RA and Dec (float32 degrees, ICRS J2000), V magnitude
times 100 (int16) and the HIP number (uint32).
"""

import gzip
import math
import struct
import sys
from pathlib import Path

MAG_LIMIT = 9.0
YEARS = 2000.0 - 1991.25
RECORD = struct.Struct("<ffhI")
OUT = Path(__file__).resolve().parent.parent / "celestron_aux" / "data" / "hipparcos.bin.gz"
# Columns of the HEASARC table (its line[1] header).
RA, DEC, HIP, VMAG, PM_RA, PM_DEC = 1, 2, 5, 7, 14, 15


def main(source: str) -> None:
    stars = []
    with gzip.open(source, "rt", encoding="ascii") as lines:
        in_data = False
        for line in lines:
            if line.startswith("<DATA>"):
                in_data = True
                continue
            if not in_data or line.startswith("<END>"):
                continue
            f = line.split("|")
            if not (f[RA] and f[DEC] and f[VMAG]) or float(f[VMAG]) > MAG_LIMIT:
                continue
            ra, dec, mag = float(f[RA]), float(f[DEC]), float(f[VMAG])
            pm_ra = float(f[PM_RA] or 0) / 3.6e6 * YEARS  # degrees, already times cos(dec)
            pm_dec = float(f[PM_DEC] or 0) / 3.6e6 * YEARS
            cos_dec = math.cos(math.radians(dec))
            ra = (ra + (pm_ra / cos_dec if cos_dec > 1e-9 else 0.0)) % 360
            stars.append((ra, dec + pm_dec, round(mag * 100), int(f[HIP])))
    stars.sort(key=lambda star: star[1])
    data = b"".join(RECORD.pack(*star) for star in stars)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(gzip.compress(data, compresslevel=9, mtime=0))
    print(f"{len(stars)} stars to V {MAG_LIMIT} -> {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main(sys.argv[1])
