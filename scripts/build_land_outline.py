#!/usr/bin/env python3
# celestron-aux · Copyright (C) 2021-2026 Patricio Latini · SPDX-License-Identifier: GPL-3.0-or-later
"""Build celestron_aux/data/land.json, the continents drawn on the page's Earth sphere.

Input: Natural Earth's 1:110m land polygons (public domain), ne_110m_land.geojson from
https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_land.geojson

    python3 scripts/build_land_outline.py path/to/ne_110m_land.geojson

Writes each polygon's outer outline as a list of [longitude, latitude] pairs
rounded to 0.1 degree, which is far finer than a globe a few hundred pixels
across can show.
"""

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "celestron_aux" / "data" / "land.json"


def main(source: str) -> None:
    features = json.loads(Path(source).read_text())["features"]
    outlines = []
    for feature in features:
        geometry = feature["geometry"]
        polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
        for polygon in polygons:
            ring = []
            for lon, lat in polygon[0]:
                point = [round(lon, 1), round(lat, 1)]
                if not ring or point != ring[-1]:
                    ring.append(point)
            if len(ring) >= 4:
                outlines.append(ring)
    OUT.write_text(json.dumps({"source": "Natural Earth 1:110m land (public domain)", "outlines": outlines},
                              separators=(",", ":")) + "\n")
    print(f"{len(outlines)} outlines, {sum(map(len, outlines))} points -> {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main(sys.argv[1])
