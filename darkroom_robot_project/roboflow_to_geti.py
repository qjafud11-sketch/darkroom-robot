"""Roboflow COCO zip → Geti용 Datumaro zip.

Roboflow 내보내기는 표준 COCO가 아니다.

  sample_detection.v3i.coco.zip                 압축 루트에 train/valid/test
  sample_detection.v3i.coco-segmentation.zip    한 겹 폴더 안에 train/valid/test
  각 split 에 이미지 + _annotations.coco.json

클래스 id 0 더미(이름 중복)를 빼지 않으면 Datumaro/Geti 가 실패한다.
인스턴스 세그는 coco-segmentation zip 을 쓴다.

  source .venv/bin/activate
  pip install datumaro
  python roboflow_to_geti.py
  python roboflow_to_geti.py ~/Downloads/sample_detection.v3i.coco-segmentation.zip
"""
from __future__ import annotations

import argparse
import json
import shutil
import zipfile
from pathlib import Path

DOWNLOADS = Path.home() / "Downloads"
CAPTURES = Path.home() / "darkroom_captures" / "geti_sample_seg"
DEFAULTS = (
    DOWNLOADS / "sample_detection.v3i.coco-segmentation.zip",
    DOWNLOADS / "sample_detection.v3i.coco-segmentation",
    DOWNLOADS / "sample_detection.v3i.coco.zip",
    DOWNLOADS / "sample_detection.v3i.coco",
)
SPLITS = ("train", "valid", "test", "val")
ANN_NAME = "_annotations.coco.json"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _has_splits(folder: Path) -> bool:
    return any((folder / name / ANN_NAME).is_file() for name in SPLITS)


def find_dataset_root(src: Path, extract_dir: Path) -> Path:
    if src.is_file() and src.suffix.lower() == ".zip":
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(src) as zf:
            zf.extractall(extract_dir)
        folder = extract_dir
    elif src.is_dir():
        folder = src
    else:
        raise FileNotFoundError(f"Roboflow COCO 경로가 없습니다: {src}")

    if _has_splits(folder):
        return folder
    children = [p for p in folder.iterdir() if p.is_dir() and _has_splits(p)]
    if len(children) == 1:
        return children[0]
    raise FileNotFoundError(
        f"{src} 안에 train/valid/test + {ANN_NAME} 가 없습니다. Roboflow COCO zip 인지 확인하세요."
    )


def clean_annotations(src_root: Path, dest_root: Path) -> None:
    """더미 클래스 0을 빼고 이미지를 연결한다."""
    found = False
    for split in SPLITS:
        src = src_root / split
        ann = src / ANN_NAME
        if not ann.is_file():
            continue
        found = True
        dest = dest_root / split
        dest.mkdir(parents=True, exist_ok=True)
        data = json.loads(ann.read_text(encoding="utf-8"))
        cats = [cat for cat in data.get("categories", []) if int(cat.get("id", -1)) != 0]
        if not cats:
            cats = list(data.get("categories", []))
        for cat in cats:
            if cat.get("name") == "sample-detection":
                cat["name"] = "sample"
            cat["supercategory"] = ""
        data["categories"] = cats
        (dest / ANN_NAME).write_text(json.dumps(data), encoding="utf-8")
        for image in src.iterdir():
            if image.suffix.lower() in IMAGE_EXT:
                target = dest / image.name
                if not target.exists():
                    target.symlink_to(image.resolve())
    if not found:
        raise FileNotFoundError(f"{src_root} 에 {ANN_NAME} 가 없습니다.")


def to_datumaro(clean_root: Path, out_dir: Path) -> None:
    from datumaro.components.annotation import AnnotationType
    from datumaro.components.dataset import Dataset

    ds = Dataset.import_from(str(clean_root), "roboflow_coco")
    items = []
    for item in ds:
        subset = "val" if item.subset == "valid" else item.subset
        polygons = [ann for ann in item.annotations if ann.type == AnnotationType.polygon]
        boxes = [ann for ann in item.annotations if ann.type == AnnotationType.bbox]
        anns = polygons if polygons else boxes
        items.append(item.wrap(subset=subset, annotations=anns))
    ds = Dataset.from_iterable(items, categories=ds.categories())
    if out_dir.exists():
        shutil.rmtree(out_dir)
    ds.export(str(out_dir), "datumaro", save_media=True)


def zip_contents(folder: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(folder).as_posix())


def pick_source(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    for candidate in DEFAULTS:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "Downloads 에 sample_detection.v3i.coco-segmentation.zip (또는 .coco.zip) 이 없습니다."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Roboflow COCO zip을 Geti Datumaro zip으로 변환")
    parser.add_argument("source", nargs="?", default="", help="Roboflow coco zip 또는 푼 폴더")
    parser.add_argument(
        "-o",
        "--output",
        default="",
        help="Geti에 올릴 zip 경로. 기본은 원본 옆 sample_detection.v3i.datumaro.zip",
    )
    args = parser.parse_args()
    source = pick_source(args.source or None)
    work = CAPTURES / "_work"
    if work.exists():
        shutil.rmtree(work)
    extract_dir = work / "extract"
    clean_dir = work / "clean"
    result_dir = CAPTURES / "datumaro_result"

    print(f"[geti] 입력  {source}")
    root = find_dataset_root(source, extract_dir)
    print(f"[geti] Roboflow 루트  {root}")
    clean_annotations(root, clean_dir)
    to_datumaro(clean_dir, result_dir)
    dest = Path(args.output).expanduser() if args.output else source.with_name("sample_detection.v3i.datumaro.zip")
    if dest.suffix.lower() != ".zip":
        dest = dest / "sample_detection.v3i.datumaro.zip"
    zip_contents(result_dir, dest)
    shutil.rmtree(work, ignore_errors=True)
    print(f"[geti] Datumaro 폴더  {result_dir}")
    print(f"[geti] Geti 업로드 zip  {dest}")
    print("Import dataset 에 이 zip 을 그대로 올리면 됩니다.")


if __name__ == "__main__":
    main()
