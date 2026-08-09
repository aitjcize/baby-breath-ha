"""Generate the add-on icon.png and logo.png (nursery-nightlight style)."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

NIGHT = (32, 17, 10)  # BGR #0A1120
AMBER = (138, 201, 255)  # BGR #FFC98A
MINT = (195, 224, 123)  # BGR #7BE0C3
INK = (230, 239, 244)  # BGR #F4EFE6

OUT_DIR = Path(__file__).resolve().parent.parent / "baby_respiration"


def rounded_mask(width: int, height: int, radius: int) -> np.ndarray:
    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.rectangle(mask, (radius, 0), (width - radius, height), 255, -1)
    cv2.rectangle(mask, (0, radius), (width, height - radius), 255, -1)
    for cx, cy in ((radius, radius), (width - radius, radius), (radius, height - radius), (width - radius, height - radius)):
        cv2.circle(mask, (cx, cy), radius, 255, -1)
    return mask


def glow(canvas: np.ndarray, center: tuple[int, int], radius: int, color: tuple[int, int, int], strength: float) -> None:
    overlay = np.zeros_like(canvas)
    cv2.circle(overlay, center, radius, color, -1, cv2.LINE_AA)
    overlay = cv2.GaussianBlur(overlay, (0, 0), radius / 2.5)
    np.add(canvas, (overlay * strength).astype(canvas.dtype), out=canvas, casting="unsafe")


def crescent(canvas: np.ndarray, center: tuple[int, int], radius: int) -> None:
    moon = np.zeros(canvas.shape[:2], dtype=np.uint8)
    cv2.circle(moon, center, radius, 255, -1, cv2.LINE_AA)
    bite_center = (center[0] - int(radius * 0.42), center[1] - int(radius * 0.38))
    cv2.circle(moon, bite_center, int(radius * 0.92), 0, -1, cv2.LINE_AA)
    canvas[moon > 0] = AMBER


def breathing_wave(canvas: np.ndarray, y_base: int, amplitude: int, thickness: int, x0: int, x1: int, cycles: float) -> None:
    xs = np.arange(x0, x1)
    ys = (y_base - amplitude * np.sin((xs - x0) / (x1 - x0) * cycles * 2 * np.pi)).astype(np.int32)
    points = np.stack([xs, ys], axis=1).reshape(-1, 1, 2)
    under = np.zeros_like(canvas)
    cv2.polylines(under, [points], False, MINT, thickness + 10, cv2.LINE_AA)
    under = cv2.GaussianBlur(under, (0, 0), 6)
    np.add(canvas, (under * 0.45).astype(canvas.dtype), out=canvas, casting="unsafe")
    cv2.polylines(canvas, [points], False, MINT, thickness, cv2.LINE_AA)


def stars(canvas: np.ndarray, seed: int, count: int) -> None:
    rng = np.random.default_rng(seed)
    height, width = canvas.shape[:2]
    for _ in range(count):
        x = int(rng.uniform(12, width - 12))
        y = int(rng.uniform(10, height * 0.45))
        r = int(rng.integers(1, 3))
        cv2.circle(canvas, (x, y), r, INK, -1, cv2.LINE_AA)


def make_icon() -> None:
    size = 256
    canvas = np.full((size, size, 3), NIGHT, dtype=np.uint8)
    glow(canvas, (176, 84), 70, AMBER, 0.25)
    crescent(canvas, (176, 84), 46)
    stars(canvas, seed=7, count=6)
    breathing_wave(canvas, y_base=186, amplitude=22, thickness=9, x0=28, x1=228, cycles=1.5)
    alpha = rounded_mask(size, size, 56)
    icon = np.dstack([canvas, alpha])
    cv2.imwrite(str(OUT_DIR / "icon.png"), icon)


def make_logo() -> None:
    width, height = 500, 200
    canvas = np.full((height, width, 3), NIGHT, dtype=np.uint8)
    glow(canvas, (86, 84), 56, AMBER, 0.25)
    crescent(canvas, (86, 84), 38)
    stars(canvas, seed=11, count=5)
    breathing_wave(canvas, y_base=150, amplitude=14, thickness=6, x0=40, x1=460, cycles=2.5)
    cv2.putText(canvas, "Baby Respiration", (158, 78), cv2.FONT_HERSHEY_DUPLEX, 1.05, INK, 2, cv2.LINE_AA)
    cv2.putText(canvas, "gentle motion monitor", (160, 106), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (182, 160, 147), 1, cv2.LINE_AA)
    alpha = rounded_mask(width, height, 28)
    cv2.imwrite(str(OUT_DIR / "logo.png"), np.dstack([canvas, alpha]))


if __name__ == "__main__":
    make_icon()
    make_logo()
    print(f"wrote {OUT_DIR / 'icon.png'} and {OUT_DIR / 'logo.png'}")
