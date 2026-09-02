"""결함이 패널 전체에서 가장 센 반응인지 훑어본다.

상자 두 개(결함/깨끗한 곳)만 비교하면 고른 자리에 따라 점수가 흔들린다.
검출기는 화면 어디든 볼 수 있으니, 패널 전체를 같은 크기 창으로 훑어서
결함 창이 몇 등인지 본다. 1등이고 2등과 벌어져 있으면 오검 위험이 낮다.

calib_score 와 같은 규칙을 따른다 — 모델이 보는 크기로 줄인 뒤 부동소수로 잰다.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from calib_score import TOP_FRACTION, local_contrast, to_model_scale


def _window_score(feature, y0, y1, x0, x1, frac=TOP_FRACTION):
    patch = feature[y0:y1, x0:x1].ravel()
    keep = max(8, int(patch.size * frac))
    return float(np.sort(patch)[-keep:].mean())


def scan(image, defect_box, panel_box, step=8, model_side=640, sigma=1.5):
    """결함 창과 같은 크기로 패널을 훑는다.

    결함과 겹치는 창은 배경에서 뺀다. 안 그러면 결함 자신이 배경 1등이 된다.
    """
    small, scale = to_model_scale(image.convert("RGB"), model_side)
    feature = local_contrast(np.asarray(small.convert("L"), dtype=np.float64), sigma)

    dx0, dy0, dx1, dy1 = (round(v * scale) for v in defect_box)
    px0, py0, px1, py1 = (round(v * scale) for v in panel_box)
    win_h, win_w = dy1 - dy0, dx1 - dx0

    defect = _window_score(feature, dy0, dy1, dx0, dx1)

    scores = []
    best = None
    for y in range(py0, py1 - win_h + 1, step):
        for x in range(px0, px1 - win_w + 1, step):
            overlap = not (x + win_w <= dx0 or x >= dx1 or y + win_h <= dy0 or y >= dy1)
            if overlap:
                continue
            value = _window_score(feature, y, y + win_h, x, x + win_w)
            scores.append(value)
            if best is None or value > best[0]:
                best = (value, x, y)

    clean = np.array(scores)
    return {
        "defect": defect,
        "clean_max": float(clean.max()),
        "clean_mean": float(clean.mean()),
        "clean_std": float(clean.std()),
        "ratio": defect / max(float(clean.max()), 1e-6),
        "zscore": (defect - float(clean.mean())) / max(float(clean.std()), 1e-6),
        "beaten_by": int((clean >= defect).sum()),
        "windows": int(clean.size),
        "worst_spot": (int(best[1] / scale), int(best[2] / scale)) if best else None,
        "window_px": (int(win_w / scale), int(win_h / scale)),
    }


def heatmap(image, panel_box, model_side=640, sigma=1.5):
    """국소 대비 지도를 그림으로. 어디가 세게 반응하는지 눈으로 본다."""
    small, scale = to_model_scale(image.convert("RGB"), model_side)
    feature = local_contrast(np.asarray(small.convert("L"), dtype=np.float64), sigma)
    px0, py0, px1, py1 = (round(v * scale) for v in panel_box)
    mask = np.zeros_like(feature)
    mask[py0:py1, px0:px1] = feature[py0:py1, px0:px1]
    top = np.percentile(mask[mask > 0], 99.5) if (mask > 0).any() else 1.0
    norm = np.clip(mask / max(top, 1e-6), 0, 1)
    return Image.fromarray((norm * 255).astype(np.uint8), mode="L")
