#!/usr/bin/env python3
"""Generate the deterministic synthetic fixture video.

The fixture exists so the whole product can be demonstrated and tested with
**no camera, no GPU and no network**. It is drawn, not filmed: simple moving
figures crossing a marked area. It exercises decode, rate control, tracking,
zone logic, event generation, persistence and the browser ingest path — it does
**not** demonstrate detection accuracy, and it is labelled as synthetic
everywhere it is used.

Usage:
    python scripts/make_fixture.py [--out samples/fixtures/zone-walkthrough.mp4]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

WIDTH, HEIGHT = 320, 240
FPS = 10
SECONDS = 12
#: The marked restricted area, in normalised coordinates.
ZONE = (0.34, 0.30, 0.70, 0.92)


def draw_scene(frame_index: int, total: int) -> np.ndarray:
    image = np.full((HEIGHT, WIDTH, 3), (28, 26, 24), dtype=np.uint8)
    # Floor and wall give the tracker a stable background to work against.
    cv2.rectangle(image, (0, int(HEIGHT * 0.62)), (WIDTH, HEIGHT), (58, 54, 48), -1)
    cv2.line(image, (0, int(HEIGHT * 0.62)), (WIDTH, int(HEIGHT * 0.62)), (90, 84, 74), 1)
    for x in range(0, WIDTH, 40):
        cv2.line(image, (x, int(HEIGHT * 0.62)), (x, HEIGHT), (70, 64, 56), 1)

    # The zone the pipeline is expected to reason about.
    x1, y1, x2, y2 = ZONE
    zx1, zy1 = int(x1 * WIDTH), int(y1 * HEIGHT)
    zx2, zy2 = int(x2 * WIDTH), int(y2 * HEIGHT)
    overlay = image.copy()
    cv2.rectangle(overlay, (zx1, zy1), (zx2, zy2), (40, 120, 220), -1)
    cv2.addWeighted(overlay, 0.18, image, 0.82, 0, image)
    cv2.rectangle(image, (zx1, zy1), (zx2, zy2), (40, 120, 220), 2)
    cv2.putText(
        image, "restricted", (zx1 + 4, zy1 + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (60, 150, 240), 1
    )

    cv2.putText(
        image,
        f"SYNTHETIC FIXTURE  frame {frame_index:04d}/{total}",
        (6, HEIGHT - 8),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.32,
        (170, 170, 170),
        1,
    )

    # One figure walks left to right through the zone, then returns.
    progress = frame_index / max(1, total - 1)
    ping_pong = 1.0 - abs(2.0 * progress - 1.0)
    figure_x = int(0.06 * WIDTH + ping_pong * (WIDTH - 0.30))
    _draw_figure(image, figure_x, int(HEIGHT * 0.62))
    return image


def _draw_figure(image: np.ndarray, x: int, ground_y: int) -> None:
    """A simple, human-shaped silhouette that moves as one rigid figure."""
    head_r = 7
    cv2.circle(image, (x, ground_y - 44), head_r, (196, 176, 156), -1)
    body_top = ground_y - 36
    body_bottom = ground_y - 16
    cv2.rectangle(image, (x - 7, body_top), (x + 7, body_bottom), (72, 96, 148), -1)
    cv2.line(image, (x - 7, body_top + 3), (x - 11, ground_y - 12), (72, 96, 148), 4)
    cv2.line(image, (x + 7, body_top + 3), (x + 11, ground_y - 12), (72, 96, 148), 4)
    cv2.line(image, (x - 4, body_bottom), (x - 5, ground_y), (48, 62, 92), 4)
    cv2.line(image, (x + 4, body_bottom), (x + 5, ground_y), (48, 62, 92), 4)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="samples/fixtures/zone-walkthrough.mp4",
        help="output path (default: samples/fixtures/zone-walkthrough.mp4)",
    )
    args = parser.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    total = FPS * SECONDS
    # mp4v keeps the file small and decodable by opencv-python everywhere.
    writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (WIDTH, HEIGHT))
    if not writer.isOpened():
        raise SystemExit(f"could not open a video writer for {out}")
    for index in range(total):
        writer.write(draw_scene(index, total))
    writer.release()

    size_kb = out.stat().st_size / 1024
    print(f"wrote {out} — {WIDTH}x{HEIGHT} @ {FPS}fps, {total} frames, {size_kb:.0f} KB")
    print("This clip is SYNTHETIC (drawn shapes, not filmed people). It demonstrates the")
    print("pipeline, zone logic and event flow — not detection accuracy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
