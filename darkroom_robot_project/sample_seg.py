"""샘플 테두리 YOLO11-seg.

Roboflow sample_detection 라벨로 학습한 마스크로 물체만 남긴다.
스크래치·찌그러짐 YOLO(crop640)와 파일이 다르다.

  python sample_seg.py /path/to.jpg
"""
from __future__ import annotations

import os
from pathlib import Path
from PIL import Image
import numpy as np

DEFAULT_WEIGHTS = Path.home() / "darkroom_models" / "yolo" / "sample_seg" / "weights" / "best.pt"
IMGSZ = 640
CONF = 0.25

_model = None
_device: str | None = None


def weights_path() -> Path:
    raw = os.environ.get("SAMPLE_SEG_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    best = DEFAULT_WEIGHTS
    last = best.with_name("last.pt")
    if best.is_file():
        return best
    return last


def available() -> bool:
    return weights_path().is_file()


def pick_device() -> str:
    global _device
    if _device is None:
        from yolo_train import pick_device as _pick

        _device = _pick()
    return _device


def load_model(path: str | Path | None = None):
    global _model
    weights = Path(path) if path else weights_path()
    if _model is not None and str(getattr(_model, "ckpt_path", "")) == str(weights):
        return _model
    if not weights.is_file():
        raise FileNotFoundError(f"샘플 세그 가중치 없음: {weights}")
    from ultralytics import YOLO

    model = YOLO(str(weights))
    model.ckpt_path = str(weights)
    _model = model
    return model


def warmup(path: str | Path | None = None) -> str:
    model = load_model(path)
    device = pick_device()
    dummy = np.zeros((IMGSZ, IMGSZ, 3), dtype=np.uint8)
    model.predict(source=dummy, imgsz=IMGSZ, device=device, verbose=False, retina_masks=True)
    print(f"[sample-seg] 로드 {weights_path()}  장치 {device}", flush=True)
    return str(weights_path())


def predict_mask(image: Image.Image | np.ndarray | str | Path, conf: float = CONF) -> np.ndarray | None:
    """원본 크기 bool 마스크. 없으면 None."""
    if not available():
        return None
    if isinstance(image, (str, Path)):
        rgb = Image.open(image).convert("RGB")
    elif isinstance(image, Image.Image):
        rgb = image.convert("RGB")
    else:
        rgb = Image.fromarray(np.asarray(image)).convert("RGB")
    w, h = rgb.size
    arr = np.asarray(rgb)
    model = load_model()
    results = model.predict(
        source=arr,
        imgsz=IMGSZ,
        conf=conf,
        device=pick_device(),
        verbose=False,
        retina_masks=True,
        max_det=5,
    )
    if not results:
        return None
    result = results[0]
    if result.masks is None or len(result.masks) == 0:
        return None
    scores = result.boxes.conf.detach().cpu().numpy() if result.boxes is not None else None
    idx = int(np.argmax(scores)) if scores is not None and len(scores) else 0
    mask = result.masks.data[idx].detach().cpu().numpy()
    mask = (mask > 0.5).astype(np.uint8) * 255
    resized = Image.fromarray(mask, mode="L").resize((w, h), Image.NEAREST)
    return np.asarray(resized) > 127


def warmup_safe() -> str | None:
    try:
        from sample_ov import warmup_safe as ov_warm

        ov = ov_warm()
        if ov:
            return ov
    except Exception as exc:
        print(f"[sample-seg] Geti 로드 실패: {exc}", flush=True)
    try:
        return warmup()
    except Exception as exc:
        print(f"[sample-seg] 로드 실패: {exc}", flush=True)
        return None


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="샘플 세그 한 장")
    parser.add_argument("image")
    args = parser.parse_args()
    mask = predict_mask(args.image)
    print("ok" if mask is not None and mask.any() else "miss", int(mask.sum()) if mask is not None else 0)
