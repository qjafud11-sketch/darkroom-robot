"""수집 샘플의 양품/불량 표시.

비지도는 양품만 모은다. 샘플 단위로 OK/NG 를 남기고,
unsup_prepare.py 가 카메라·면별로 학습 폴더를 만든다.

목표:
  60  양품 세트 — 면마다 학습 50 + 홀드아웃 10
  30  첫 학습을 돌려볼 최소 (면마다 학습 ~25 + 홀드아웃 ~5)

한 세트 = 6장(1차 카메라 1~4, 2차 카메라 3·4). 조명·캘리브가 바뀌면 처음부터다.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from camera import CAPTURE_DIR

DATASET_DIR = CAPTURE_DIR / "dataset"  # 기존 sample_* 어노테이션
OKSET_DIR = CAPTURE_DIR / "okset"  # 예전 양품수집 ok_###
CROPSET_DIR = CAPTURE_DIR / "cropset"  # Geti 크롭 수집 crop_1, crop_2, ...
LABELS_PATH = CAPTURE_DIR / "collect_labels.json"
UNSUP_DIR = CAPTURE_DIR / "unsup"
COLLECT_PREF_PATH = CAPTURE_DIR / "collect_prefs.json"

NEW_PREFIX = "ok"
NEW_NAME = re.compile(rf"{NEW_PREFIX}_(\d+)$")
CROP_PREFIX = "crop"
CROP_NAME = re.compile(rf"{CROP_PREFIX}_(\d+)$")

TARGET_OK = 60
MIN_OK = 30
TEST_OK = 10

FACES = (
    {"key": "face1_cam1_s1", "name": "면 1", "stage": 1, "cam": 1},
    {"key": "face2_cam2_s1", "name": "면 2", "stage": 1, "cam": 2},
    {"key": "face3_cam3_s1", "name": "면 3", "stage": 1, "cam": 3},
    {"key": "face4_cam4_s1", "name": "면 4", "stage": 1, "cam": 4},
    {"key": "face5_cam3_s2", "name": "면 5", "stage": 2, "cam": 3},
    {"key": "face6_cam4_s2", "name": "면 6", "stage": 2, "cam": 4},
)


def shot_path(folder: Path, stage: int, cam: int) -> Path:
    return folder / f"{folder.name}_{stage}_{cam}.jpg"


def is_complete(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    return all(shot_path(folder, face["stage"], face["cam"]).is_file() for face in FACES)


def sample_dir(name: str) -> Path:
    """sample_* 는 기존 dataset, 나머지는 okset."""
    if name.startswith("sample_"):
        return DATASET_DIR / name
    return OKSET_DIR / name


def load_collect_root() -> Path:
    if COLLECT_PREF_PATH.is_file():
        try:
            data = json.loads(COLLECT_PREF_PATH.read_text(encoding="utf-8"))
            raw = str(data.get("root") or "").strip()
            if raw:
                return Path(raw).expanduser()
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return CROPSET_DIR


def save_collect_root(root: str | Path) -> Path:
    path = Path(root).expanduser()
    COLLECT_PREF_PATH.parent.mkdir(parents=True, exist_ok=True)
    COLLECT_PREF_PATH.write_text(
        json.dumps({"root": str(path), "updated_at": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def next_crop_name(root: str | Path | None = None) -> str:
    """저장 폴더에서 crop_N 다음 번호. crop_1, crop_2, ..."""
    folder = Path(root).expanduser() if root else load_collect_root()
    used = set()
    if folder.exists():
        for item in folder.iterdir():
            match = CROP_NAME.fullmatch(item.name)
            if item.is_dir() and match:
                used.add(int(match.group(1)))
    number = 1
    while number in used:
        number += 1
    return f"{CROP_PREFIX}_{number}"


def list_crops(root: str | Path | None = None) -> list[Path]:
    folder = Path(root).expanduser() if root else load_collect_root()
    if not folder.exists():
        return []
    items = [p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")]

    def key(path: Path):
        match = CROP_NAME.fullmatch(path.name)
        return (0, int(match.group(1))) if match else (1, path.name)

    return sorted(items, key=key)


def next_ok_name() -> str:
    """okset 에서 ok_### 다음 번호. 기존 sample_* 와 겹치지 않는다."""
    used = set()
    if OKSET_DIR.exists():
        for item in OKSET_DIR.iterdir():
            match = NEW_NAME.fullmatch(item.name)
            if item.is_dir() and match:
                used.add(int(match.group(1)))
    number = 1
    while number in used:
        number += 1
    return f"{NEW_PREFIX}_{number:03d}"


def list_samples() -> list[Path]:
    roots = [DATASET_DIR, OKSET_DIR, CROPSET_DIR, load_collect_root()]
    seen: set[Path] = set()
    folders = []
    for root in roots:
        try:
            resolved = root.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen or not root.exists():
            continue
        seen.add(resolved)
        folders.extend(
            p for p in root.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )
    return sorted(folders, key=lambda p: p.name)


def next_unlabeled_in(root: str | Path) -> str | None:
    for folder in list_crops(root):
        if is_complete(folder) and get_verdict(folder.name) not in ("OK", "NG"):
            return folder.name
    return None


def count_ok_in(root: str | Path) -> int:
    n = 0
    for folder in list_crops(root):
        if get_verdict(folder.name) == "OK" and is_complete(folder):
            n += 1
    return n


def progress_text_in(root: str | Path) -> str:
    folders = list_crops(root)
    complete = [folder for folder in folders if is_complete(folder)]
    ok = sum(1 for folder in complete if get_verdict(folder.name) == "OK")
    unlabeled = sum(1 for folder in complete if get_verdict(folder.name) not in ("OK", "NG"))
    return (
        f"이 폴더 양품 {ok} / 목표 {TARGET_OK}  ·  완료 {len(complete)}세트  ·  "
        f"미표시 {unlabeled}개"
    )


def load_labels() -> dict[str, Any]:
    if not LABELS_PATH.is_file():
        return {"samples": {}}
    try:
        data = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"samples": {}}
    samples = data.get("samples")
    if not isinstance(samples, dict):
        samples = {}
    return {"samples": samples}


def save_labels(data: dict[str, Any]) -> None:
    LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "target_ok": TARGET_OK,
        "min_ok": MIN_OK,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "samples": data.get("samples") or {},
    }
    LABELS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def set_verdict(sample_name: str, verdict: str, note: str = "") -> dict[str, Any]:
    verdict = str(verdict).strip().upper()
    if verdict not in ("OK", "NG"):
        raise ValueError(f"verdict 는 OK 또는 NG: {verdict!r}")
    data = load_labels()
    data["samples"][sample_name] = {
        "verdict": verdict,
        "note": note,
        "labeled_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_labels(data)
    return data["samples"][sample_name]


def get_verdict(sample_name: str) -> str:
    item = load_labels()["samples"].get(sample_name) or {}
    return str(item.get("verdict") or "").upper()


def count_ok() -> int:
    samples = load_labels()["samples"]
    n = 0
    for folder in list_samples():
        if get_verdict(folder.name) != "OK":
            continue
        if is_complete(folder):
            n += 1
    return n


def next_unlabeled() -> str | None:
    """6장이 있는데 아직 OK/NG 가 없는 샘플 이름."""
    for folder in list_samples():
        if is_complete(folder) and get_verdict(folder.name) not in ("OK", "NG"):
            return folder.name
    return None


def progress_text() -> str:
    ok = count_ok()
    unlabeled = sum(
        1 for folder in list_samples()
        if is_complete(folder) and get_verdict(folder.name) not in ("OK", "NG")
    )
    return (
        f"양품 {ok} / 목표 {TARGET_OK}  ·  최소 {MIN_OK}  ·  "
        f"미표시 완료 {unlabeled}개"
    )
