"""도색 색상별 배경 노이즈 비교.

3D 프린팅 바닥면 텍스처가 배경 노이즈의 주범이라, 면을 칠해서 눌러보려 한다.
어느 색이 나은지 보려면 두 가지를 갈라야 한다.

- 텍스처(고정 패턴): 프레임을 여러 장 평균해도 남는다. 도색이 실제로 표면을 메웠는지 본다.
- 센서 노이즈(시간 축): 프레임마다 달라진다. 밝기가 낮으면 상대적으로 커진다.

절대값만 보면 어두운 색이 무조건 이긴다. 빛이 덜 돌아오니 텍스처 진폭도 같이 줄어서다.
그래서 밝기로 나눈 상대 텍스처를 같이 본다 — 이게 도색이 표면을 메운 정도다.
"""
from __future__ import annotations

import subprocess

import numpy as np
from PIL import Image

from calib_score import gaussian_blur, to_model_scale

# 표면 얼룩은 몇 픽셀 크기다. 이보다 크게 잡으면 패널 전체 음영까지 노이즈로 센다.
TEXTURE_SIGMA = 2.0


def grab(device, path, frames=3):
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", "1280x720", "-framerate", "10",
            "-i", device,
            "-frames:v", str(frames), "-update", "1", "-q:v", "2", "-y", path,
        ],
        capture_output=True,
        timeout=20,
    )
    return path


def _residual_std(plane, sigma=TEXTURE_SIGMA):
    """고주파 잔차의 표준편차. 얼룩·알갱이 세기."""
    return float((plane - gaussian_blur(plane, sigma)).std())


def measure(frames, box, model_side=None):
    """한 구역의 밝기·텍스처·센서 노이즈.

    frames 는 같은 자리를 찍은 여러 장. 평균 영상에 남는 게 텍스처고,
    장마다 흔들리는 게 센서 노이즈다.
    """
    x0, y0, x1, y1 = box
    stack = []
    for image in frames:
        img = image
        if model_side:
            img, scale = to_model_scale(img, model_side)
            sx0, sy0, sx1, sy1 = (round(v * scale) for v in box)
        else:
            sx0, sy0, sx1, sy1 = x0, y0, x1, y1
        plane = np.asarray(img.convert("L"), dtype=np.float64)[sy0:sy1, sx0:sx1]
        stack.append(plane)
    cube = np.stack(stack)

    fixed = cube.mean(axis=0)
    mean = float(fixed.mean())
    texture = _residual_std(fixed)
    temporal = float(cube.std(axis=0).mean())
    return {
        "mean": mean,
        "texture": texture,
        "texture_rel": texture / max(mean, 1e-6),
        "temporal": temporal,
    }


def channel_means(frames, box):
    """조명 아래에서 각 채널이 얼마나 돌아오는지. 색 선택의 근거."""
    x0, y0, x1, y1 = box
    cube = np.stack([
        np.asarray(f.convert("RGB"), dtype=np.float64)[y0:y1, x0:x1] for f in frames
    ])
    flat = cube.mean(axis=0)
    return {ch: float(flat[:, :, i].mean()) for i, ch in enumerate("RGB")}
