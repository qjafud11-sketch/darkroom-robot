"""캘리브 값을 실제로 카메라에 넣고 AI 분리도로 채점하는 측정 도구.

calib_ui 로 눈으로 맞추는 것과 달리, 여기서는 흠집 상자와 깨끗한 면 상자를 주고
calib_score 로 숫자를 받는다. 조합을 돌려 어느 값이 검출기에 유리한지 고를 때 쓴다.

UI 가 카메라를 잡고 있으면 안 된다. 한 번에 한 대만 연다.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from PIL import Image

from calib_score import exposure_health, score
from camera_calib import set_controls

FRAME_DIR = Path("/tmp/calib_probe")

# 카메라 2 (판 앞면, 흠집 있는 면) 기준 상자. 1280x720 좌표.
CAM2_DEFECT = (655, 150, 895, 350)
CAM2_CLEAN = [
    (380, 120, 610, 330),
    (380, 345, 620, 400),
    (930, 120, 1080, 330),
    (655, 360, 895, 405),
]
CAM2_SURFACE = [(370, 110, 1085, 400)]


def grab(device, tag, width=1280, height=720, fps=10, quality=2):
    """검사 촬영과 같은 경로로 한 장 받는다."""
    FRAME_DIR.mkdir(exist_ok=True)
    out = FRAME_DIR / f"{tag}.jpg"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", f"{width}x{height}", "-framerate", str(fps),
            "-i", device, "-frames:v", "8", "-q:v", str(quality), "-update", "1",
            "-y", str(out),
        ],
        check=True,
        timeout=45,
    )
    return out


def shoot(device, tag, controls, settle=0.45, **grab_kwargs):
    set_controls(device, controls)
    time.sleep(settle)
    path = grab(device, tag, **grab_kwargs)
    return Image.open(path).convert("RGB"), path


def measure(device, tag, controls, frames=3, settle=0.45, prepare=None, **grab_kwargs):
    """같은 설정으로 여러 장 찍어 평균을 낸다.

    한 장만 재면 프레임 잡음이 설정 차이보다 커서 순위가 뒤집힌다.
    prepare 를 주면 채점 전에 사진을 한 번 가공한다 (소프트웨어 필터 비교용).
    """
    set_controls(device, controls)
    time.sleep(settle)
    rows = []
    paths = []
    for index in range(frames):
        path = grab(device, f"{tag}_f{index}", **grab_kwargs)
        image = Image.open(path).convert("RGB")
        if prepare is not None:
            image = prepare(image)
        rows.append(rate(image))
        paths.append(path)
    out = {key: sum(row[key] for row in rows) / len(rows) for key in rows[0]}
    out["dprime_spread"] = max(r["dprime"] for r in rows) - min(r["dprime"] for r in rows)
    out["paths"] = paths
    return out


def rate(image, defect=CAM2_DEFECT, clean=CAM2_CLEAN, surface=CAM2_SURFACE):
    s = score(image, defect, clean)
    h = exposure_health(image, surface)
    return {
        "dprime": s["dprime"],
        "margin": s["margin"],
        "mean": h["mean"],
        "clip": h["clip"],
        "crush": h["crush"],
    }


def healthy(row, mean_range=(120, 215), clip=1.0, crush=1.0):
    lo, hi = mean_range
    return row["clip"] < clip and row["crush"] < crush and lo <= row["mean"] <= hi


def line(label, row):
    return (
        f"{label:<30} dprime {row['dprime']:6.2f}  margin {row['margin']:6.2f}  "
        f"면평균 {row['mean']:6.1f}  포화 {row['clip']:5.2f}%  뭉개짐 {row['crush']:5.2f}%"
    )
