"""양품 라벨을 비지도 학습 폴더로 보낸다.

면(카메라·차수)마다 Anomalib/MVTec 형식으로 나눈다.

  ~/darkroom_captures/unsup/face1_cam1_s1/train/good/
  ~/darkroom_captures/unsup/face1_cam1_s1/test/good/

양품 세트를 번호 순으로 정렬한 뒤, 앞·뒤가 섞이게 일정 간격으로
TEST_OK(기본 10)개를 test, 나머지는 train. 마지막만 떼면 나중에 찍은
조명·구도가 문턱만 올리고 학습에는 안 들어간다.

사용:
  python unsup_prepare.py          # 현황 출력 후 폴더 갱신
  python unsup_prepare.py --status # 현황만
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from dataset_label import (
    CAPTURE_DIR,
    FACES,
    MIN_OK,
    NEW_PREFIX,
    TARGET_OK,
    TEST_OK,
    UNSUP_DIR,
    get_verdict,
    is_complete,
    list_flat_complete_names,
    list_samples,
    progress_text,
    shot_path,
)

FLAT_OK_DIR = CAPTURE_DIR / "ok"
from sample_roi import crop_camera, write_roi_index


def _is_flat_root(root: Path) -> bool:
    return any(root.glob(f"{NEW_PREFIX}_*_1_1.jpg"))


def _ok_complete(root: Path | None = None, assume_ok: bool = False) -> list[tuple[Path, str]]:
    """(폴더, 샘플이름). 평평한 ok_N_차_캠.jpg 또는 예전 샘플 하위 폴더."""
    if root is not None and root.is_dir() and _is_flat_root(root):
        names = list_flat_complete_names(root)
        items = []
        for name in names:
            if assume_ok or get_verdict(name) == "OK":
                items.append((root, name))
        return items
    items = []
    for folder in list_samples():
        if not is_complete(folder):
            continue
        if assume_ok or get_verdict(folder.name) == "OK":
            items.append((folder, folder.name))
    return items


def _split(items: list[tuple[Path, str]]) -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
    if not items:
        return [], []
    hold = min(TEST_OK, max(1, len(items) // 6))
    if len(items) <= hold + 5:
        hold = max(1, len(items) // 5) if len(items) >= 5 else 0
    if hold <= 0:
        return items, []
    step = max(2, len(items) // hold)
    test = items[step - 1 :: step][:hold]
    test_set = set(test)
    train = [p for p in items if p not in test_set]
    return train, test


def _save_crop(src: Path, dest: Path, cam_id: int, stage: int = 1) -> tuple[int, int, int, int]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    _rgb, result = crop_camera(src, cam_id, stage=stage)
    result.image.save(dest, format="JPEG", quality=95, subsampling=0)
    return result.box


def export_unsup(root: Path | None = None, assume_ok: bool = False) -> dict:
    ok_items = _ok_complete(root, assume_ok=assume_ok)
    train_items, test_items = _split(ok_items)
    if UNSUP_DIR.exists():
        shutil.rmtree(UNSUP_DIR)
    UNSUP_DIR.mkdir(parents=True, exist_ok=True)

    copied = {face["key"]: {"train": 0, "test": 0} for face in FACES}
    boxes = {face["key"]: [] for face in FACES}
    skipped = []
    for split_name, items in (("train", train_items), ("test", test_items)):
        for folder, name in items:
            for face in FACES:
                src = shot_path(folder, face["stage"], face["cam"], sample=name)
                dest = UNSUP_DIR / face["key"] / split_name / "good" / src.name
                try:
                    box = _save_crop(src, dest, face["cam"], stage=face["stage"])
                except Exception as exc:
                    skipped.append(f"{src.name}: {exc}")
                    continue
                copied[face["key"]][split_name] += 1
                boxes[face["key"]].append(box)

    roi = write_roi_index(boxes)
    summary = {
        "ok_sets": len(ok_items),
        "train_sets": len(train_items),
        "test_sets": len(test_items),
        "target_ok": TARGET_OK,
        "min_ok": MIN_OK,
        "source": str(root) if root is not None else "",
        "assume_ok": assume_ok,
        "root": str(UNSUP_DIR),
        "faces": copied,
        "crop": "fov+oriented",
        "skipped": skipped[:40],
        "roi": roi.get("faces"),
    }
    (UNSUP_DIR / "split.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def print_status(root: Path | None = None, assume_ok: bool = False) -> None:
    if root is not None and root.is_dir() and _is_flat_root(root):
        names = list_flat_complete_names(root)
        ok = _ok_complete(root, assume_ok=assume_ok)
        print(f"평평한 수집  {root}  ·  6면 완료 {len(names)}세트  ·  학습에 쓸 양품 {len(ok)}")
        return
    folders = list_samples()
    complete = [p for p in folders if is_complete(p)]
    ok = _ok_complete(root, assume_ok=assume_ok)
    ng = [p for p in complete if get_verdict(p.name) == "NG"]
    unlabeled = [p for p in complete if get_verdict(p.name) not in ("OK", "NG")]
    print(progress_text())
    print(f"완료 샘플 {len(complete)}  ·  양품 {len(ok)}  ·  불량 {len(ng)}  ·  미표시 {len(unlabeled)}")
    if unlabeled:
        names = ", ".join(p.name for p in unlabeled[:12])
        extra = f" 외 {len(unlabeled) - 12}개" if len(unlabeled) > 12 else ""
        print(f"미표시: {names}{extra}")
    remain_min = max(0, MIN_OK - len(ok))
    remain_target = max(0, TARGET_OK - len(ok))
    if remain_min:
        print(f"첫 학습까지 양품 {remain_min}세트 더 필요 (최소 {MIN_OK})")
    elif remain_target:
        print(f"첫 학습은 가능. 목표까지 양품 {remain_target}세트 더 (목표 {TARGET_OK})")
    else:
        print(f"목표 {TARGET_OK}세트 충족 — unsup 폴더를 갱신하면 된다")


def main():
    parser = argparse.ArgumentParser(description="양품을 비지도 학습 폴더로 보낸다")
    parser.add_argument("--status", action="store_true", help="폴더는 건드리지 않고 숫자만")
    parser.add_argument("--root", default="", help="평평한 수집 폴더. 예: ~/darkroom_captures/ok")
    parser.add_argument("--assume-ok", action="store_true", help="라벨 없이 완료 세트를 전부 양품으로 본다")
    args = parser.parse_args()
    raw = str(args.root or "").strip()
    root = Path(raw).expanduser() if raw else (FLAT_OK_DIR if FLAT_OK_DIR.is_dir() else None)
    assume_ok = bool(args.assume_ok or (root is not None and _is_flat_root(root)))
    print_status(root, assume_ok=assume_ok)
    if args.status:
        return
    summary = export_unsup(root, assume_ok=assume_ok)
    print(
        f"내보냄  {summary['root']}  "
        f"train {summary['train_sets']}세트  test {summary['test_sets']}세트  (샘플 크롭)"
    )
    skipped = summary.get("skipped") or []
    if skipped:
        print(f"크롭 건너뜀 {len(skipped)}")
        for line in skipped[:12]:
            print(f"  {line}")
    for face in FACES:
        counts = summary["faces"][face["key"]]
        print(f"  {face['name']} {face['key']}  train {counts['train']}  test {counts['test']}")


if __name__ == "__main__":
    main()
