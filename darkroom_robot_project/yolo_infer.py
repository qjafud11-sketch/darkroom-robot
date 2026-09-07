"""크롭된 샘플 면에서 scratch/dent 를 찾는다.

기본은 Geti OpenVINO 검출 (YOLOX 등, scratch/dent).
  ~/darkroom_models/openvino/defect_det/model.xml
없으면 Ultralytics YOLO 가중치
  ~/darkroom_models/yolo/crop640/weights/best.pt

전처리는 학습과 같다: FOV → 샘플 크롭. 박스는 검사 벽면 좌표로 되돌린다.

  python yolo_infer.py /path/to.jpg --cam 1
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

OV_XML = Path.home() / "darkroom_models" / "openvino" / "defect_det" / "model.xml"
PT_WEIGHTS = Path.home() / "darkroom_models" / "yolo" / "crop640" / "weights" / "best.pt"
DEFAULT_WEIGHTS = OV_XML
IMGSZ = 640
OV_LABELS = ("scratch", "dent")
OV_CONF_DEFAULT = 0.60
OV_PAD = 114

CLASS_ALIAS = {
    "scratch": "scratch",
    "dent": "dent",
    "찌그러짐": "dent",
    "스크래치": "scratch",
}

_model = None
_ov_compiled = None
_ov_path: str | None = None
_ov_meta: dict[str, Any] | None = None
_device: str | None = None


def _parse_ov_meta(xml: Path) -> dict[str, Any]:
    import xml.etree.ElementTree as ET

    info: dict[str, Any] = {
        "labels": list(OV_LABELS),
        "pad": OV_PAD,
        "reverse": True,
        "scale": 1.0,
        "conf": OV_CONF_DEFAULT,
        "iou": 0.5,
        "resize": "fit_to_window_letterbox",
        "size": IMGSZ,
    }
    try:
        root = ET.parse(xml).getroot()
    except (OSError, ET.ParseError):
        return info
    model_info = root.find("rt_info/model_info")
    if model_info is None:
        return info
    labels_el = model_info.find("labels")
    labels = ((labels_el.get("value") if labels_el is not None else "") or "").split()
    if labels:
        info["labels"] = [_norm_class(name) for name in labels]
    pad = model_info.find("pad_value")
    if pad is not None and pad.get("value"):
        info["pad"] = int(float(pad.get("value")))
    rev = model_info.find("reverse_input_channels")
    if rev is not None and rev.get("value"):
        info["reverse"] = str(rev.get("value")).lower() in ("true", "1", "yes")
    scale = model_info.find("scale_values")
    if scale is not None and scale.get("value"):
        info["scale"] = float(str(scale.get("value")).split()[0])
    conf = model_info.find("confidence_threshold")
    if conf is not None and conf.get("value"):
        info["conf"] = float(conf.get("value"))
    iou = model_info.find("iou_threshold")
    if iou is not None and iou.get("value"):
        info["iou"] = float(iou.get("value"))
    resize = model_info.find("resize_type")
    if resize is not None and resize.get("value"):
        info["resize"] = str(resize.get("value"))
    return info


def ov_meta() -> dict[str, Any]:
    global _ov_meta
    xml = weights_path()
    if _ov_meta is not None and _ov_path == str(xml):
        return _ov_meta
    _ov_meta = _parse_ov_meta(xml) if _is_openvino(xml) else {
        "labels": list(OV_LABELS),
        "pad": OV_PAD,
        "reverse": True,
        "scale": 1.0,
        "conf": OV_CONF_DEFAULT,
        "iou": 0.5,
        "resize": "fit_to_window_letterbox",
        "size": IMGSZ,
    }
    return _ov_meta


def _is_openvino(path: str | Path) -> bool:
    return Path(path).suffix.lower() == ".xml"


def weights_path() -> Path:
    raw = os.environ.get("JUDGE_MODEL_PATH", "").strip()
    if raw:
        path = Path(raw).expanduser()
        if path.is_dir():
            xml = path / "model.xml"
            if xml.is_file():
                return xml
        return path
    if OV_XML.is_file() and OV_XML.with_suffix(".bin").is_file():
        return OV_XML
    return PT_WEIGHTS


def pick_device() -> str:
    global _device
    if _device is not None:
        return _device
    if _is_openvino(weights_path()):
        import openvino as ov

        forced = os.environ.get("JUDGE_OV_DEVICE", "").strip()
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
    from yolo_train import pick_device as _pick

    _device = _pick()
    return _device


def _norm_class(name: str) -> str:
    key = (name or "").strip().lower()
    return CLASS_ALIAS.get(key, key or "unknown")


def _letterbox(image, size: int = IMGSZ, pad: int = OV_PAD):
    """Geti/OTX fit_to_window_letterbox. 패딩 114, 가운데 정렬."""
    from PIL import Image

    w, h = image.size
    scale = min(size / w, size / h)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    canvas = Image.new("RGB", (size, size), (pad, pad, pad))
    px = (size - nw) // 2
    py = (size - nh) // 2
    canvas.paste(image.resize((nw, nh), Image.BILINEAR), (px, py))
    return canvas, scale, px, py


def _box_to_xyxy(box, w: int, h: int, scale: float = 1.0, pad_x: int = 0, pad_y: int = 0) -> list[int]:
    x1, y1, x2, y2 = [float(v) for v in box]
    mx = max(abs(x1), abs(y1), abs(x2), abs(y2))
    if mx <= 2.0:
        x1, x2 = x1 * IMGSZ, x2 * IMGSZ
        y1, y2 = y1 * IMGSZ, y2 * IMGSZ
    if scale <= 0:
        scale = 1.0
    x1 = (x1 - pad_x) / scale
    x2 = (x2 - pad_x) / scale
    y1 = (y1 - pad_y) / scale
    y2 = (y2 - pad_y) / scale
    return [
        int(max(0, min(w, round(min(x1, x2))))),
        int(max(0, min(h, round(min(y1, y2))))),
        int(max(0, min(w, round(max(x1, x2))))),
        int(max(0, min(h, round(max(y1, y2))))),
    ]


def _iou(a: list[int], b: list[int]) -> float:
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter <= 0:
        return 0.0
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    area_b = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union else 0.0


def _nms(hits: list[dict[str, Any]], iou_thr: float = 0.5) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for item in hits:
        if any(
            item["class_name"] == other["class_name"] and _iou(item["bbox"], other["bbox"]) >= iou_thr
            for other in kept
        ):
            continue
        kept.append(item)
    return kept


def _load_openvino(xml: Path):
    global _ov_compiled, _ov_path
    if _ov_compiled is not None and _ov_path == str(xml):
        return _ov_compiled
    if not xml.is_file():
        raise FileNotFoundError(f"결함 OpenVINO 모델 없음: {xml}")
    import openvino as ov

    core = ov.Core()
    compiled = core.compile_model(core.read_model(xml), pick_device())
    _ov_compiled = compiled
    _ov_path = str(xml)
    global _ov_meta
    _ov_meta = _parse_ov_meta(xml)
    return compiled


def load_model(path: str | Path | None = None):
    global _model
    weights = Path(path) if path else weights_path()
    if _is_openvino(weights):
        return _load_openvino(weights)
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

    weights = Path(path) if path else weights_path()
    device = pick_device()
    if _is_openvino(weights):
        compiled = _load_openvino(weights)
        dummy = np.zeros((1, 3, IMGSZ, IMGSZ), dtype=np.float32)
        compiled(dummy)
        meta = ov_meta()
        print(
            f"[yolo] OpenVINO 검출 로드 {weights}  장치 {device}  "
            f"letterbox pad={meta['pad']}  "
            f"{'BGR' if meta['reverse'] else 'RGB'}  "
            f"scale={meta['scale']}  conf={meta['conf']}",
            flush=True,
        )
        return str(weights)
    model = load_model(weights)
    dummy = np.zeros((IMGSZ, IMGSZ, 3), dtype=np.uint8)
    model.predict(source=dummy, imgsz=IMGSZ, device=device, verbose=False)
    print(f"[yolo] 로드 {weights}  장치 {device}", flush=True)
    return str(weights)


def _ov_detect(work_rgb, conf: float) -> list[dict[str, Any]]:
    import numpy as np
    from PIL import Image

    compiled = _load_openvino(weights_path())
    meta = ov_meta()
    image = work_rgb if isinstance(work_rgb, Image.Image) else Image.fromarray(np.asarray(work_rgb)).convert("RGB")
    w, h = image.size
    canvas, scale, pad_x, pad_y = _letterbox(image, pad=int(meta["pad"]))
    arr = np.asarray(canvas)
    if meta["reverse"]:
        arr = arr[:, :, ::-1]
    arr = arr.astype(np.float32)
    scale_val = float(meta["scale"]) or 1.0
    if scale_val != 1.0:
        arr = arr / scale_val
    x = np.ascontiguousarray(arr.transpose(2, 0, 1)[None])
    out = compiled(x)
    names = []
    for item in compiled.outputs:
        try:
            names.append(item.get_any_name())
        except Exception:
            names.append("")
    def _tensor(key: str, *alts: str):
        for name in (key, *alts):
            if name in names:
                return np.asarray(out[compiled.output(name)])
        raise KeyError(f"OpenVINO 출력 없음: {key} (있는 것: {names})")

    raw_boxes = _tensor("boxes", "bboxes")
    labels = np.asarray(_tensor("labels"))
    if raw_boxes.ndim == 3:
        raw_boxes = raw_boxes[0]
    if labels.ndim == 2:
        labels = labels[0]
    if raw_boxes.shape[-1] >= 5:
        scores = raw_boxes[:, 4]
        boxes = raw_boxes[:, :4]
    else:
        scores = np.asarray(_tensor("scores"))
        if scores.ndim == 2:
            scores = scores[0]
        boxes = raw_boxes
    hits: list[dict[str, Any]] = []
    for box, lab, score in zip(boxes, labels, scores):
        score = float(score)
        if score < conf:
            continue
        lab = int(lab)
        names_map = list(meta.get("labels") or OV_LABELS)
        raw_name = names_map[lab] if 0 <= lab < len(names_map) else str(lab)
        class_name = _norm_class(raw_name)
        if class_name not in ("scratch", "dent"):
            continue
        x1, y1, x2, y2 = _box_to_xyxy(box, w, h, scale, pad_x, pad_y)
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        hits.append(
            {
                "class_name": class_name,
                "score": score,
                "bbox": [x1, y1, x2, y2],
            }
        )
    hits.sort(key=lambda item: item["score"], reverse=True)
    return _nms(hits, iou_thr=float(meta.get("iou") or 0.5))


def _pt_detect(work_bgr, conf: float) -> list[dict[str, Any]]:
    model = load_model()
    device = pick_device()
    results = model.predict(
        source=work_bgr,
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
    for box in boxes:
        xyxy = box.xyxy[0].tolist()
        local = [int(xyxy[0]), int(xyxy[1]), int(xyxy[2]), int(xyxy[3])]
        cls_id = int(box.cls[0])
        score = float(box.conf[0])
        raw_name = names.get(cls_id, str(cls_id))
        class_name = _norm_class(str(raw_name))
        if class_name not in ("scratch", "dent"):
            continue
        hits.append({"class_name": class_name, "score": score, "bbox": local})
    hits.sort(key=lambda item: item["score"], reverse=True)
    return hits


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
        raw = os.environ.get("JUDGE_SCORE_MIN", "").strip()
        if raw:
            try:
                conf = float(raw)
            except ValueError:
                conf = None
        if conf is None:
            conf = OV_CONF_DEFAULT
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

    if _is_openvino(weights_path()):
        raw_hits = _ov_detect(work, conf)
    else:
        raw_hits = _pt_detect(arr[:, :, ::-1].copy(), conf)

    hits: list[dict[str, Any]] = []
    full_w, full_h = rgb.size
    for item in raw_hits:
        local = list(item["bbox"])
        bbox = map_bbox_to_full(local, crop_box, full_w, full_h, affine=affine)
        hits.append(
            {
                "class_name": item["class_name"],
                "score": item["score"],
                "bbox": bbox,
                "crop_bbox": local,
                "ng": True,
            }
        )
    return hits


def main():
    parser = argparse.ArgumentParser(description="결함 한 장 검출 (OpenVINO / YOLO)")
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
