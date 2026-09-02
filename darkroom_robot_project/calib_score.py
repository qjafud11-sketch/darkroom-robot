"""검사 사진이 AI 모델 눈에 얼마나 구분되는지 점수를 낸다.

사람 눈이 아니라 YOLO 같은 검출기 기준이다. 그래서 세 가지를 지킨다.

1. 모델이 실제로 보는 크기에서 재기. 1280 원본에서 잘 보여도 640으로 줄이면
   얇은 흠집은 사라진다. 그래서 MODEL_SIDE 로 줄인 뒤에 측정한다.
2. 흠집이 "밝다/어둡다"가 아니라 "깨끗한 면의 최악 지점보다 세다"를 본다.
   검출기는 결국 문턱을 하나 긋는 거라, 배경 최댓값과의 간격이 성능을 정한다.
3. 부동소수로 재기. 흠집 진폭이 5계조밖에 안 돼서, 8비트 정수로 재면
   양자화 잡음이 설정 차이보다 커진다.

쓰는 쪽은 흠집 상자 하나와 깨끗한 면 상자 여러 개를 준다.
측정은 calib_probe 가 여러 장 평균으로 부른다 — 한 장은 프레임 잡음이 심하다.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

MODEL_SIDE = 640
TOP_FRACTION = 0.05


def gaussian_blur(plane, sigma):
    """분리형 가우시안. 가장자리는 값을 늘려 채운다(반사 대신 복제)."""
    radius = max(1, int(round(sigma * 3)))
    offsets = np.arange(-radius, radius + 1, dtype=np.float64)
    kernel = np.exp(-(offsets ** 2) / (2.0 * sigma * sigma))
    kernel /= kernel.sum()

    out = plane
    for axis in (0, 1):
        padded = np.pad(out, [(radius, radius) if a == axis else (0, 0) for a in (0, 1)], mode="edge")
        stacked = np.stack(
            [
                padded[i:i + out.shape[0]] if axis == 0 else padded[:, i:i + out.shape[1]]
                for i in range(kernel.size)
            ]
        )
        out = np.tensordot(kernel, stacked, axes=(0, 0))
    return out


def to_model_scale(image, model_side=MODEL_SIDE):
    """검출기가 보는 크기로 줄인다. 배율과 함께 돌려준다."""
    scale = model_side / max(image.size)
    if scale >= 1.0:
        return image, 1.0
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.LANCZOS), scale


def _scale_box(box, scale):
    return tuple(round(v * scale) for v in box)


def local_contrast(plane, sigma=1.5):
    """국소 대비 지도. 검출기 첫 층이 보는 것과 비슷한 신호."""
    return np.abs(plane - gaussian_blur(plane, sigma))


def _top_values(plane, box, frac=TOP_FRACTION):
    """센 쪽 frac 만 남긴다. 흠집은 상자 안에서도 일부 픽셀이다."""
    x0, y0, x1, y1 = box
    patch = plane[y0:y1, x0:x1].ravel()
    keep = max(8, int(patch.size * frac))
    return np.sort(patch)[-keep:]


def score(image, defect_box, clean_boxes, model_side=MODEL_SIDE, sigma=1.5):
    """분리도를 낸다.

    dprime  흠집 응답과 배경 응답이 몇 표준편차 떨어져 있나. 클수록 좋다.
    margin  흠집 최댓값 / 배경 최댓값. 1 이하면 배경에 묻힌다.
    """
    small, scale = to_model_scale(image.convert("RGB"), model_side)
    plane = np.asarray(small.convert("L"), dtype=np.float64)
    feature = local_contrast(plane, sigma)

    defect = _top_values(feature, _scale_box(defect_box, scale))
    clean = np.concatenate(
        [_top_values(feature, _scale_box(box, scale)) for box in clean_boxes]
    )

    spread = float(np.sqrt((defect.var() + clean.var()) / 2.0)) or 1e-6
    return {
        "dprime": float(defect.mean() - clean.mean()) / spread,
        "margin": float(defect.max()) / max(float(clean.max()), 1e-6),
        "defect": float(defect.mean()),
        "clean": float(clean.mean()),
        "size": small.size,
    }


def exposure_health(image, surface_boxes):
    """면이 타거나 뭉개지지 않았나. 어느 값이든 이건 먼저 통과해야 한다."""
    plane = np.asarray(image.convert("L"), dtype=np.uint8)
    patches = [plane[y0:y1, x0:x1].ravel() for x0, y0, x1, y1 in surface_boxes]
    pixels = np.concatenate(patches)
    return {
        "mean": float(pixels.mean()),
        "clip": 100.0 * float((pixels >= 250).mean()),
        "crush": 100.0 * float((pixels <= 5).mean()),
    }
