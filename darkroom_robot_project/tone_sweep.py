"""조명 톤·밝기를 바꿔가며 검출도와 색 중성도를 함께 잰다.

카메라는 기본값 근처에 두는 게 목표다. 그래서 조명 쪽에서 색을 맞춘다.
NeoPixel 흰색(r=g=b)은 파랗게 치우쳐서, 그걸 카메라 화이트밸런스로 되돌리면
약한 채널에 이득을 크게 걸어 노이즈를 같이 키운다. 조명 채널비로 잡으면
화이트밸런스가 할 일이 없어져 노이즈가 안 늘어난다.

스윕 중에는 노출과 화이트밸런스를 수동으로 묶는다. 자동으로 두면 조명을 바꿀 때마다
카메라가 따라 움직여서 톤의 효과가 가려진다. 고른 뒤에 노출만 다시 맞춘다.
"""
from __future__ import annotations

import subprocess
import time

import numpy as np
from PIL import Image

from arduino_link import send_ok
from calib_score import gaussian_blur
from camera_calib import list_controls, set_controls
from defect_scan import scan
from light_tone import tone_command

FRAMES = 4
SETTLE = 0.6


def base_controls(device, exposure, wb_temp=None):
    """장치 기본값에서 노출만 수동으로 묶은 설정.

    화이트밸런스는 wb_temp 를 주면 수동으로 고정한다. 스윕 중에는 고정해야
    톤 차이가 보인다. 실제 운영에서는 자동으로 둬도 된다.
    """
    values = {
        item["name"]: item["default"]
        for item in list_controls(device)
        if not item.get("read_only")
    }
    values["auto_exposure"] = 1
    values["exposure_time_absolute"] = int(exposure)
    if wb_temp is not None:
        values["white_balance_automatic"] = 0
        values["white_balance_temperature"] = int(wb_temp)
    return values


def grab(device, path, frames=3):
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", "1280x720", "-framerate", "10",
            "-i", device, "-frames:v", str(frames),
            "-update", "1", "-q:v", "2", "-y", path,
        ],
        capture_output=True,
        timeout=20,
    )
    return Image.open(path).convert("RGB")


def shoot(device, count=FRAMES, tag="ts"):
    return [grab(device, f"/tmp/{tag}_{i}.jpg") for i in range(count)]


def rate(frames, defect_box, panel_box):
    """색 중성도 · 노출 건강 · 결함 분리도 · 표면 텍스처를 한 번에."""
    x0, y0, x1, y1 = panel_box
    rgb = np.stack([np.asarray(f, dtype=np.float64)[y0:y1, x0:x1] for f in frames])
    panel = rgb.mean(axis=0)
    chans = [float(panel[:, :, i].mean()) for i in range(3)]

    luma = np.stack([
        np.asarray(f.convert("L"), dtype=np.float64)[y0:y1, x0:x1] for f in frames
    ])
    flat = luma.mean(axis=0)
    texture = float((flat - gaussian_blur(flat, 2.0)).std())

    whole = np.asarray(frames[0].convert("L"), dtype=np.uint8)[y0:y1, x0:x1]
    hits = [scan(f, defect_box, panel_box, step=8) for f in frames]

    return {
        "R": chans[0],
        "G": chans[1],
        "B": chans[2],
        # 채널 최대/최소 비. 1.0 이면 완전 중성이고, 크면 그만큼 색이 치우쳤다.
        "cast": max(chans) / max(min(chans), 1e-6),
        "mean": float(flat.mean()),
        "clip": 100.0 * float((whole >= 250).mean()),
        "texture": texture,
        "ratio": float(np.mean([h["ratio"] for h in hits])),
        "zscore": float(np.mean([h["zscore"] for h in hits])),
        "defect": float(np.mean([h["defect"] for h in hits])),
        "clean_max": float(np.mean([h["clean_max"] for h in hits])),
    }


def probe(device, controls, command, defect_box, panel_box, tag="ts"):
    """조명 명령 + 카메라 설정 하나를 넣고 측정값을 돌려준다."""
    send_ok(command)
    set_controls(device, controls)
    time.sleep(SETTLE)
    return rate(shoot(device, tag=tag), defect_box, panel_box)


def header():
    return (
        f"{'조건':<18} {'R':>6} {'G':>6} {'B':>6} {'치우침':>7} "
        f"{'밝기':>6} {'포화%':>6} {'텍스처':>7} {'결함':>7} {'배경':>6} {'비율':>6} {'z':>7}"
    )


def line(label, m):
    return (
        f"{label:<18} {m['R']:6.1f} {m['G']:6.1f} {m['B']:6.1f} {m['cast']:7.2f} "
        f"{m['mean']:6.1f} {m['clip']:6.2f} {m['texture']:7.2f} "
        f"{m['defect']:7.2f} {m['clean_max']:6.2f} {m['ratio']:6.2f} {m['zscore']:7.1f}"
    )
