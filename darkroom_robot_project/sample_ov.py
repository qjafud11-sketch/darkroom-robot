"""Geti OpenVINO 샘플 면 분할.

Geti에서 보낸 lite_hrnet_s (의미 분할). 입력 512×512, 출력 3채널
softmax — 0 배경, 1 sample-detection, 2 Empty.

  ~/darkroom_models/openvino/sample_seg/model.xml
  없으면 ~/Downloads/OpenVINO_model/model.xml
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image
import numpy as np

DEFAULT_XML = Path.home() / "darkroom_models" / "openvino" / "sample_seg" / "model.xml"
DOWNLOAD_XML = Path.home() / "Downloads" / "OpenVINO_model" / "model.xml"
IMGSZ = 512
MEAN = np.array([123.675, 116.28, 103.53], dtype=np.float32)
SCALE = np.array([58.395, 57.12, 57.375], dtype=np.float32)
SAMPLE_CH = 1
SOFT_THR = 0.5

_compiled = None
_device: str | None = None


def weights_path() -> Path:
    raw = os.environ.get("SAMPLE_OV_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    if DEFAULT_XML.is_file():
        return DEFAULT_XML
    return DOWNLOAD_XML


def available() -> bool:
    return weights_path().is_file()


def pick_device() -> str:
    global _device
    if _device is not None:
        return _device
    import openvino as ov

    forced = os.environ.get("SAMPLE_OV_DEVICE", "").strip()
    core = ov.Core()
    devices = list(core.available_devices)
    if forced and forced in devices:
        _device = forced
        return _device
    for name in reversed(devices):
        if name.startswith("GPU"):
            _device = name
            return _device
    _device = "CPU" if "CPU" in devices else (devices[0] if devices else "CPU")
    return _device


def load_model(path: str | Path | None = None):
    global _compiled
    xml = Path(path) if path else weights_path()
    if _compiled is not None and str(getattr(_compiled, "xml_path", "")) == str(xml):
        return _compiled
    if not xml.is_file():
        raise FileNotFoundError(f"Geti OpenVINO 모델 없음: {xml}")
    import openvino as ov

    core = ov.Core()
    device = pick_device()
    compiled = core.compile_model(core.read_model(xml), device)
    compiled.xml_path = str(xml)
    _compiled = compiled
    return compiled


def warmup(path: str | Path | None = None) -> str:
    model = load_model(path)
    dummy = np.zeros((1, 3, IMGSZ, IMGSZ), dtype=np.float32)
    model(dummy)
    print(f"[sample-ov] 로드 {weights_path()}  장치 {pick_device()}", flush=True)
    return str(weights_path())


def warmup_safe() -> str | None:
    try:
        return warmup()
    except Exception as exc:
        print(f"[sample-ov] 로드 실패: {exc}", flush=True)
        return None


def _as_rgb(image: Image.Image | np.ndarray | str | Path) -> Image.Image:
    if isinstance(image, (str, Path)):
        return Image.open(image).convert("RGB")
    if isinstance(image, Image.Image):
        return image.convert("RGB")
    return Image.fromarray(np.asarray(image)).convert("RGB")


def predict_mask(image: Image.Image | np.ndarray | str | Path, conf: float = SOFT_THR) -> np.ndarray | None:
    """원본 크기 bool 마스크. 샘플 면만 True."""
    if not available():
        return None
    rgb = _as_rgb(image)
    w, h = rgb.size
    arr = np.asarray(rgb.resize((IMGSZ, IMGSZ), Image.BILINEAR), dtype=np.float32)
    x = ((arr - MEAN) / SCALE).transpose(2, 0, 1)[None]
    compiled = load_model()
    out = compiled(x)[compiled.output(0)]
    if out.ndim != 4 or out.shape[1] < 2:
        return None
    prob = out[0, SAMPLE_CH]
    mask = Image.fromarray((prob > float(conf)).astype(np.uint8) * 255, mode="L")
    mask = mask.resize((w, h), Image.NEAREST)
    raw = np.asarray(mask) > 127
    return raw if raw.any() else None


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Geti OpenVINO 샘플 마스크")
    parser.add_argument("image")
    args = parser.parse_args()
    mask = predict_mask(args.image)
    print("ok" if mask is not None and mask.any() else "miss", int(mask.sum()) if mask is not None else 0)
