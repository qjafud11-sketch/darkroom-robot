"""Roboflow YOLO 데이터를 샘플 크롭 데이터로 바꾼다.

  - 전체 1280×720 이 아니라 비지도와 같은 sample_roi 크롭
  - normal 클래스 제거. 양품은 박스 없는 음성 샘플
  - 크롭 해상도는 그대로 둔다 (256으로 줄이지 않음. 학습 때 640)

입력:  /home/intel/Downloads/darkroom.v2i.yolov8
출력:  ~/darkroom_captures/yolo/

  python yolo_prepare.py
  python yolo_prepare.py --src /path/to/darkroom.v2i.yolov8
"""
from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

from sample_roi import crop_detected, map_bbox_to_crop

SRC_DEFAULT = Path("/home/intel/Downloads/darkroom.v2i.yolov8")
OUT_DEFAULT = Path.home() / "darkroom_captures" / "yolo"
KEEP_CLASSES = ("Scratch", "dent")
DROP_CLASS = "normal"
MIN_KEEP_FRAC = 0.25


def _load_names(src: Path) -> list[str]:
    yaml = src / "data.yaml"
    names: list[str] = []
    for line in yaml.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("names:"):
            raw = line.split(":", 1)[1].strip()
            names = [n.strip(" '\"") for n in raw.strip("[]").split(",") if n.strip()]
            break
    if not names:
        names = ["Scratch", "dent", "normal"]
    return names


def _read_yolo(path: Path) -> list[tuple[int, float, float, float, float]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        cls = int(float(parts[0]))
        cx, cy, w, h = (float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4]))
        rows.append((cls, cx, cy, w, h))
    return rows


def _xyxy_full(cx: float, cy: float, w: float, h: float, iw: int, ih: int) -> list[float]:
    return [
        (cx - w / 2.0) * iw,
        (cy - h / 2.0) * ih,
        (cx + w / 2.0) * iw,
        (cy + h / 2.0) * ih,
    ]


def _to_yolo(box: list[int], cw: int, ch: int) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = box
    bw = max(1, x1 - x0)
    bh = max(1, y1 - y0)
    return (
        (x0 + x1) / 2.0 / cw,
        (y0 + y1) / 2.0 / ch,
        bw / cw,
        bh / ch,
    )


def convert_split(
    src: Path,
    dest: Path,
    split: str,
    src_names: list[str],
    keep_index: dict[int, int],
) -> dict:
    img_dir = src / split / "images"
    lab_dir = src / split / "labels"
    out_img = dest / split / "images"
    out_lab = dest / split / "labels"
    out_img.mkdir(parents=True, exist_ok=True)
    out_lab.mkdir(parents=True, exist_ok=True)
    preview = dest / "preview" / split
    preview.mkdir(parents=True, exist_ok=True)

    boxes_in = Counter()
    boxes_out = Counter()
    dropped_normal = 0
    dropped_clip = 0
    n_neg = 0
    n_pos = 0
    images = sorted(p for p in img_dir.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
    for img_path in images:
        rgb = Image.open(img_path).convert("RGB")
        iw, ih = rgb.size
        crop = crop_detected(rgb)
        cw, ch = crop.image.size
        stem = img_path.stem
        crop.image.save(out_img / f"{stem}.jpg", format="JPEG", quality=95, subsampling=0)

        kept: list[tuple[int, list[int]]] = []
        lab_path = lab_dir / f"{stem}.txt"
        for cls, cx, cy, w, h in _read_yolo(lab_path):
            name = src_names[cls] if 0 <= cls < len(src_names) else str(cls)
            boxes_in[name] += 1
            if cls not in keep_index:
                dropped_normal += 1
                continue
            full = _xyxy_full(cx, cy, w, h, iw, ih)
            full_area = max(1.0, (full[2] - full[0]) * (full[3] - full[1]))
            mapped = map_bbox_to_crop(full, crop.box, cw, ch, affine=crop.affine)
            if mapped is None:
                dropped_clip += 1
                continue
            mapped_area = max(1, (mapped[2] - mapped[0]) * (mapped[3] - mapped[1]))
            if mapped_area / full_area < MIN_KEEP_FRAC:
                dropped_clip += 1
                continue
            new_cls = keep_index[cls]
            kept.append((new_cls, mapped))
            boxes_out[KEEP_CLASSES[new_cls]] += 1

        lines = []
        for new_cls, box in kept:
            ycx, ycy, yw, yh = _to_yolo(box, cw, ch)
            lines.append(f"{new_cls} {ycx:.6f} {ycy:.6f} {yw:.6f} {yh:.6f}")
        (out_lab / f"{stem}.txt").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        if kept:
            n_pos += 1
        else:
            n_neg += 1

        if kept and n_pos <= 8:
            vis = crop.image.copy()
            draw = ImageDraw.Draw(vis)
            for new_cls, box in kept:
                col = (255, 48, 48) if new_cls == 0 else (255, 180, 40)
                draw.rectangle(box, outline=col, width=3)
                draw.text((box[0] + 4, box[1] + 4), KEEP_CLASSES[new_cls], fill=col)
            vis.save(preview / f"{stem}.jpg", quality=85)

    return {
        "images": len(images),
        "positive": n_pos,
        "negative": n_neg,
        "boxes_in": dict(boxes_in),
        "boxes_out": dict(boxes_out),
        "dropped_normal": dropped_normal,
        "dropped_clip": dropped_clip,
    }


def export(src: Path, dest: Path) -> dict:
    src_names = _load_names(src)
    keep_index = {src_names.index(name): i for i, name in enumerate(KEEP_CLASSES) if name in src_names}
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    splits = {}
    for split in ("train", "valid", "test"):
        if not (src / split / "images").is_dir():
            continue
        splits[split] = convert_split(src, dest, split, src_names, keep_index)
        print(
            f"{split}: {splits[split]['images']}장  "
            f"불량 {splits[split]['positive']}  양품(음성) {splits[split]['negative']}  "
            f"박스 {splits[split]['boxes_out']}"
        )
    yaml = (
        f"path: {dest}\n"
        f"train: train/images\n"
        f"val: valid/images\n"
        f"test: test/images\n"
        f"\n"
        f"nc: {len(KEEP_CLASSES)}\n"
        f"names: [{', '.join(repr(n) for n in KEEP_CLASSES)}]\n"
    )
    (dest / "data.yaml").write_text(yaml, encoding="utf-8")
    summary = {
        "src": str(src),
        "dest": str(dest),
        "classes": list(KEEP_CLASSES),
        "dropped": DROP_CLASS,
        "crop": "sample_roi",
        "resize": None,
        "splits": splits,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    (dest / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장 {dest}")
    return summary


def main():
    parser = argparse.ArgumentParser(description="YOLO 샘플 크롭 데이터 만들기")
    parser.add_argument("--src", default=str(SRC_DEFAULT))
    parser.add_argument("--dest", default=str(OUT_DEFAULT))
    args = parser.parse_args()
    src = Path(args.src)
    if not (src / "data.yaml").is_file():
        raise SystemExit(f"데이터 없음: {src}")
    export(src, Path(args.dest))


if __name__ == "__main__":
    main()
