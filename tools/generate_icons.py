#!/usr/bin/env python3
"""Render the Sentinel PWA icons from the same geometry as the SVG mark.

The favicon is an SVG, but a web app manifest needs real PNGs. Rather than
committing binaries nobody can regenerate, this script draws them with the
Pillow-free stack the backend already depends on (NumPy + OpenCV).

Usage:
    python tools/generate_icons.py [output_dir]

Defaults to frontend/public.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

# Matches --color-accent in the dark theme and the SVG favicon.
ACCENT = (255, 134, 75)  # BGR
BACKGROUND = (20, 15, 11)  # BGR, matches --color-bg (dark)

#: Shield outline on a 32x32 grid, taken from Logo.tsx so both marks agree.
SHIELD = np.array(
    [
        (16.0, 2.5),
        (5.5, 6.6),
        (5.5, 14.8),
        (7.4, 21.4),
        (11.2, 26.6),
        (16.0, 29.5),
        (20.8, 26.6),
        (24.6, 21.4),
        (26.5, 14.8),
        (26.5, 6.6),
    ],
    dtype=np.float64,
)


def render(size: int, *, maskable: bool) -> np.ndarray:
    """Draw one icon.

    ``maskable`` shrinks the mark to the 80% safe zone Android crops to, so the
    shield is never clipped by a circular or squircle mask.
    """
    supersample = 4
    canvas_size = size * supersample
    image = np.zeros((canvas_size, canvas_size, 3), dtype=np.uint8)
    image[:] = BACKGROUND

    inset = 0.62 if maskable else 0.80
    scale = canvas_size / 32.0 * inset
    offset = (canvas_size - 32.0 * scale) / 2.0

    def to_px(points: np.ndarray) -> np.ndarray:
        return np.round(points * scale + offset).astype(np.int32)

    stroke = max(1, int(round(1.8 * scale)))

    shield = to_px(SHIELD)
    overlay = image.copy()
    cv2.fillPoly(overlay, [shield], ACCENT)
    cv2.addWeighted(overlay, 0.16, image, 0.84, 0, image)
    cv2.polylines(image, [shield], isClosed=True, color=ACCENT, thickness=stroke, lineType=cv2.LINE_AA)

    centre = to_px(np.array([[16.0, 14.5]]))[0]
    cv2.circle(image, tuple(centre), int(round(5.2 * scale)), ACCENT, stroke, cv2.LINE_AA)
    cv2.circle(image, tuple(centre), int(round(1.9 * scale)), ACCENT, -1, cv2.LINE_AA)

    return cv2.resize(image, (size, size), interpolation=cv2.INTER_AREA)


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("frontend/public")
    target.mkdir(parents=True, exist_ok=True)

    outputs = [
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-512-maskable.png", 512, True),
        ("apple-touch-icon.png", 180, False),
    ]
    for name, size, maskable in outputs:
        path = target / name
        if not cv2.imwrite(str(path), render(size, maskable=maskable)):
            print(f"Failed to write {path}", file=sys.stderr)
            return 1
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
