"""학습된 YOLO11 로 스크래치·찌그러짐을 찾는다.

전처리는 학습(yolo_prepare)과 같다: FOV → 샘플 크롭. 박스는 검사 벽면
(FOV 적용 프레임) 좌표로 되돌린다.

  python yolo_infer.py /path/to.jpg --cam 1
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_WEIGHTS = Path.home() / "darkroom_models" / "yolo" / "crop640" / "weights" / "best.pt"
IMGSZ = 640

CLASS_ALIAS = {
    "scratch": "scratch",
    "dent": "dent",
    "찌그러짐": "dent",
    "스크래치": "scratch",
}

_model = None
_device: str | None = None


def weights_path() -> Path:
    raw = os.environ.get("JUDGE_MODEL_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_WEIGHTS


def pick_device() -> str:
    global _device
    if _device is None:
        from yolo_train import pick_device as _pick

        _device = _pick()
    return _device


def _norm_class(name: str) -> str:
    key = (name or "").strip().lower()
    return CLASS_ALIAS.get(key, key or "unknown")


def load_model(path: str | Path | None = None):
    global _model
    weights = Path(path) if path else weights_path()
    if _model is not None and str(getattr(_model, "ckpt_path", "")) == str(weights):
        return _model
    if not weights.is_file():
        raise FileNotFoundError(f"YOLO 가중치 없음: {weights}")
    from ultralytics import YOLO

    model = YOLO(str(weights))
    model.ckpt_path = str(weights)
    _model = model
    return model


def warmup(path: str | Path | None = None) -> str:
    import numpy as np

    model = load_model(path)
    device = pick_device()
    dummy = np.zeros((IMGSZ, IMGSZ, 3), dtype=np.uint8)
    model.predict(source=dummy, imgsz=IMGSZ, device=device, verbose=False)
    print(f"[yolo] 로드 {weights_path()}  장치 {device}", flush=True)
    return str(weights_path())


def detect_image(
    path: str | Path,
    cam_id: int | None = None,
    conf: float | None = None,
    apply_crop: bool = True,
    inspect: str | None = None,
    stage: int | None = None,
) -> list[dict[str, Any]]:
    """한 장에서 Scratch/dent 박스를 찾는다. 좌표는 FOV 프레임 기준."""
    import numpy as np

    from camera_calib import inspect_stage
    from sample_roi import crop_camera, crop_detected, map_bbox_to_full

    path = Path(path)
    if conf is None:
        raw = os.environ.get("JUDGE_SCORE_MIN", "0.25")
        try:
            conf = float(raw)
        except ValueError:
            conf = 0.25
    if stage is None:
        stage = inspect_stage(inspect) if inspect else 1

    if cam_id:
        rgb, crop = crop_camera(path, cam_id, stage=stage)
    else:
        from PIL import Image

        rgb = Image.open(path).convert("RGB")
        crop = crop_detected(rgb)
    if apply_crop:
        work = crop.image
        crop_box, affine = crop.box, crop.affine
    else:
        work = rgb
        crop_box = (0, 0, rgb.size[0], rgb.size[1])
        affine = None
    arr = np.asarray(work)
    if arr.ndim != 3 or arr.shape[2] != 3:
        return []
    bgr = arr[:, :, ::-1].copy()

    model = load_model()
    device = pick_device()
    results = model.predict(
        source=bgr,
        imgsz=IMGSZ,
        conf=conf,
        device=device,
        verbose=False,
    )
    if not results:
        return []
    result = results[0]
    names = result.names or {}
    boxes = result.boxes
    if boxes is None or len(boxes) == 0:
        return []

    hits: list[dict[str, Any]] = []
    full_w, full_h = rgb.size
    for box in boxes:
        xyxy = box.xyxy[0].tolist()
        local = [int(xyxy[0]), int(xyxy[1]), int(xyxy[2]), int(xyxy[3])]
        cls_id = int(box.cls[0])
        score = float(box.conf[0])
        raw_name = names.get(cls_id, str(cls_id))
        class_name = _norm_class(str(raw_name))
        if class_name not in ("scratch", "dent"):
            continue
        bbox = map_bbox_to_full(local, crop_box, full_w, full_h, affine=affine)
        hits.append(
            {
                "class_name": class_name,
                "score": score,
                "bbox": bbox,
                "crop_bbox": local,
                "ng": True,
            }
        )
    hits.sort(key=lambda item: item["score"], reverse=True)
    return hits


def main():
    parser = argparse.ArgumentParser(description="YOLO 한 장 검출")
    parser.add_argument("image")
    parser.add_argument("--cam", type=int, default=0)
    parser.add_argument("--conf", type=float, default=None)
    parser.add_argument("--no-crop", action="store_true", help="이미 크롭된 학습/검증 장에 사용")
    args = parser.parse_args()
    hits = detect_image(
        args.image,
        cam_id=args.cam or None,
        conf=args.conf,
        apply_crop=not args.no_crop,
    )
    print(json.dumps(hits, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
