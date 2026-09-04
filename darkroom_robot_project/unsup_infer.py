"""학습된 면별 PatchCore 로 한 장을 채점한다.

Lightning Engine 을 쓰지 않는다 (XPU accelerator 없음).
학습과 같이 256px 전처리 후 model.model() 을 직접 호출한다.

  python unsup_infer.py /path/to/ok_001_1_1.jpg --face face1_cam1_s1
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from dataset_label import FACES

MODELS_DIR = Path.home() / "darkroom_models" / "unsup"

FACE_KEY = {(face["stage"], face["cam"]): face["key"] for face in FACES}
CAM_FOR_FACE = {face["key"]: face["cam"] for face in FACES}
STAGE_FOR_FACE = {face["key"]: face["stage"] for face in FACES}

_cache: dict[str, Any] = {}
_INFER_DEVICE = None


def face_key_for(inspect: str, cam_id: int) -> str | None:
    stage = 1 if str(inspect).startswith("1") else 2 if str(inspect).startswith("2") else 0
    return FACE_KEY.get((stage, int(cam_id)))


def _infer_device():
    global _INFER_DEVICE
    if _INFER_DEVICE is None:
        from unsup_train import pick_device

        _INFER_DEVICE = pick_device()
    return _INFER_DEVICE


def load_face(face_key: str) -> dict[str, Any]:
    if face_key in _cache:
        bundle = _cache[face_key]
        thr_path = MODELS_DIR / face_key / "threshold.json"
        if thr_path.is_file():
            bundle["threshold"] = float(json.loads(thr_path.read_text(encoding="utf-8")).get("threshold") or bundle["threshold"])
        return bundle
    root = MODELS_DIR / face_key
    meta_path = root / "model.json"
    if not meta_path.is_file():
        raise FileNotFoundError(f"학습 결과 없음: {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    ckpt = Path(meta.get("ckpt") or "")
    if not ckpt.is_file():
        found = list(root.rglob("*.ckpt"))
        if not found:
            raise FileNotFoundError(f"ckpt 없음: {root}")
        ckpt = found[0]
    thr_path = root / "threshold.json"
    threshold = float(meta.get("threshold") or 1.0)
    if thr_path.is_file():
        threshold = float(json.loads(thr_path.read_text(encoding="utf-8")).get("threshold") or threshold)

    from anomalib.models import Patchcore

    device = _infer_device()
    model = Patchcore.load_from_checkpoint(str(ckpt), map_location="cpu", weights_only=False)
    model.eval()
    model.to(device)
    bundle = {
        "model": model,
        "device": device,
        "threshold": threshold,
        "meta": meta,
        "ckpt": str(ckpt),
    }
    _cache[face_key] = bundle
    return bundle


def warmup() -> list[str]:
    loaded = []
    for face in FACES:
        key = face["key"]
        try:
            load_face(key)
            loaded.append(key)
            print(f"[unsup] 로드 {key}", flush=True)
        except FileNotFoundError as exc:
            print(f"[unsup] 없음 {key}: {exc}", flush=True)
    print(f"[unsup] 장치 {_infer_device()}  {len(loaded)}/{len(FACES)}면", flush=True)
    return loaded


_SCORE_BORDER = 0.10


def _as_map(anomaly_map):
    import numpy as np

    arr = anomaly_map
    if hasattr(arr, "detach"):
        arr = arr.detach().cpu().numpy()
    arr = np.array(arr, dtype=float)
    while arr.ndim > 2:
        arr = arr[0]
    return arr


def _score_mask(rgb, map_h: int, map_w: int):
    """크롭 테두리만 점수에서 뺀다. 안쪽 스크래치·찌그러짐은 남긴다."""
    import numpy as np
    from PIL import Image

    if hasattr(rgb, "detach"):
        rgb = rgb.detach().cpu()
        if rgb.ndim == 3 and rgb.shape[0] in (1, 3):
            rgb = rgb.permute(1, 2, 0)
        rgb = (rgb.numpy() * 255.0).clip(0, 255).astype(np.uint8)
    else:
        rgb = np.asarray(rgb)
        if rgb.dtype != np.uint8:
            rgb = (np.clip(rgb, 0, 1) * 255).astype(np.uint8) if rgb.max() <= 1.5 else rgb.astype(np.uint8)
    if rgb.ndim == 2:
        h, w = rgb.shape
    else:
        h, w = rgb.shape[:2]
    mask = np.ones((h, w), dtype=bool)
    by, bx = max(1, int(h * _SCORE_BORDER)), max(1, int(w * _SCORE_BORDER))
    mask[:by, :] = False
    mask[-by:, :] = False
    mask[:, :bx] = False
    mask[:, -bx:] = False
    resized = Image.fromarray(mask.astype(np.uint8) * 255).resize((map_w, map_h), Image.NEAREST)
    return np.asarray(resized) > 127


def masked_anomaly_score(anomaly_map, rgb) -> float:
    arr = _as_map(anomaly_map)
    if arr.size == 0:
        return 0.0
    valid = _score_mask(rgb, arr.shape[0], arr.shape[1])
    if valid.any():
        return float(arr[valid].max())
    return float(arr.max())


def _heatmap_bbox(anomaly_map, orig_w: int, orig_h: int, valid=None) -> list[int]:
    import numpy as np

    arr = _as_map(anomaly_map)
    if arr.size == 0:
        return [0, 0, orig_w, orig_h]
    if valid is not None:
        arr = np.where(valid, arr, 0.0)
    hi = float(arr.max())
    if hi <= 0:
        return [0, 0, orig_w, orig_h]
    mask = arr >= (0.6 * hi)
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return [0, 0, orig_w, orig_h]
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    mh, mw = arr.shape[:2]
    sx = orig_w / max(mw, 1)
    sy = orig_h / max(mh, 1)
    return [
        max(0, int(x0 * sx)),
        max(0, int(y0 * sy)),
        min(orig_w, int((x1 + 1) * sx)),
        min(orig_h, int((y1 + 1) * sy)),
    ]


def _image_batch(path: Path, face_key: str):
    import numpy as np
    import torch
    from anomalib.data import ImageBatch

    from sample_roi import crop_camera

    cam_id = CAM_FOR_FACE.get(face_key)
    if cam_id:
        rgb, result = crop_camera(path, cam_id, stage=STAGE_FOR_FACE.get(face_key, 1))
    else:
        from PIL import Image

        from sample_roi import crop_detected

        rgb = Image.open(path).convert("RGB")
        result = crop_detected(rgb)
    cropped = result.image
    arr = np.asarray(cropped).copy()
    tensor = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).contiguous().float() / 255.0
    full_w, full_h = rgb.size
    return ImageBatch(image=tensor), full_w, full_h, result.box, cropped.size, result.affine


def score_image(path: str | Path, face_key: str) -> dict[str, Any]:
    import torch
    from unsup_train import _prepare_batch

    from sample_roi import map_bbox_to_full

    path = Path(path)
    bundle = load_face(face_key)
    model = bundle["model"]
    device = bundle["device"]
    batch, full_w, full_h, crop_box, crop_size, affine = _image_batch(path, face_key)
    batch = _prepare_batch(batch, model, device)
    model.eval()
    with torch.no_grad():
        pred = model.model(batch.image)
    amap = getattr(pred, "anomaly_map", None)
    rgb = batch.image[0]
    score = masked_anomaly_score(amap, rgb) if amap is not None else 0.0
    crop_w, crop_h = crop_size
    valid = None
    if amap is not None:
        arr = _as_map(amap)
        valid = _score_mask(rgb, arr.shape[0], arr.shape[1])
    local = _heatmap_bbox(amap, crop_w, crop_h, valid=valid) if amap is not None else [0, 0, crop_w, crop_h]
    bbox = map_bbox_to_full(local, crop_box, full_w, full_h, affine=affine)
    threshold = float(bundle["threshold"])
    return {
        "face": face_key,
        "path": str(path),
        "score": score,
        "threshold": threshold,
        "ratio": (score / threshold) if threshold > 0 else 0.0,
        "ng": score > threshold,
        "bbox": bbox,
        "crop": list(crop_box),
    }


def main():
    parser = argparse.ArgumentParser(description="PatchCore 한 장 채점")
    parser.add_argument("image")
    parser.add_argument("--face", required=True)
    args = parser.parse_args()
    result = score_image(args.image, args.face)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
