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
RAWSET_DIR = CAPTURE_DIR / "rawset"  # 원본 FOV 수집. 샘플 하위 폴더 없음
CROPPED_DIR = CAPTURE_DIR / "cropped"  # 예전 평평한 크롭 수집
FACESET_DIR = CAPTURE_DIR / "faceset"  # 예전 면별 폴더 크롭 수집
DEFECTSET_DIR = CAPTURE_DIR / "defectset"  # 결함 수집. 사진은 한 폴더, json 은 json/
CROP_CUM_DIR = CAPTURE_DIR / "crop_cum"  # 운용 중 쌓는 샘플 면 크롭
COLLECT_JSON_DIR = "json"
LABELS_PATH = CAPTURE_DIR / "collect_labels.json"
UNSUP_DIR = CAPTURE_DIR / "unsup"  # unsup_prepare 가 만든 학습 폴더
UNSUPSET_DIR = CAPTURE_DIR / "unsupset"  # 비지도 양품 수집. 사진은 한 폴더, json 은 json/
COLLECT_PREF_PATH = CAPTURE_DIR / "collect_prefs.json"

NEW_PREFIX = "ok"
NEW_NAME = re.compile(rf"{NEW_PREFIX}_(\d+)$")
CROP_PREFIX = "crop"
CROP_NAME = re.compile(rf"{CROP_PREFIX}_(\d+)$")
SAMPLE_PREFIX = "sample"
SAMPLE_NAME = re.compile(rf"{SAMPLE_PREFIX}_(\d+)$")
CROP_CUM_FILE = re.compile(r"^crop_cum_(\d+)_\d_\d\.jpg$")

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

FACE_DIRS = ("면1", "면2", "면3,5", "면4", "면6")
FACE_DIR_BY_SHOT = {
    (1, 1): "면1",
    (1, 2): "면2",
    (1, 3): "면3,5",
    (2, 3): "면3,5",
    (1, 4): "면4",
    (2, 4): "면6",
}


def face_dir_name(stage: int, cam: int) -> str:
    return FACE_DIR_BY_SHOT[(int(stage), int(cam))]


def collect_json_dir(root: str | Path) -> Path:
    return Path(root).expanduser() / COLLECT_JSON_DIR


def ensure_collect_dirs(root: str | Path) -> Path:
    folder = Path(root).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    collect_json_dir(folder).mkdir(parents=True, exist_ok=True)
    return folder


def ensure_face_dirs(root: str | Path) -> Path:
    return ensure_collect_dirs(root)


def face_shot_path(root: str | Path, stage: int, cam: int, sample: str) -> Path:
    return shot_path(Path(root).expanduser(), int(stage), int(cam), sample)


def shot_path(folder: Path, stage: int, cam: int, sample: str | None = None) -> Path:
    prefix = sample if sample else folder.name
    return folder / f"{prefix}_{stage}_{cam}.jpg"


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
    return UNSUPSET_DIR


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


def _iter_collect_jpgs(root: Path):
    if not root.exists():
        return
    for item in root.iterdir():
        if item.is_file() and item.suffix.lower() == ".jpg" and not item.name.startswith("."):
            yield item


def park_collect_manifest(folder: str | Path, name: str) -> Path | None:
    """촬영 매니페스트를 json/ 아래로 옮긴다."""
    src = Path(folder).expanduser() / name
    if not src.is_file():
        dest = collect_json_dir(folder) / name
        return dest if dest.is_file() else None
    dest = collect_json_dir(folder) / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        src.replace(dest)
    return dest


def next_flat_crop_name(root: str | Path | None = None, prefix: str = NEW_PREFIX) -> str:
    """한 폴더에 쌓인 prefix_N_차_캠.jpg 다음 번호."""
    folder = Path(root).expanduser() if root else UNSUPSET_DIR
    used = set()
    for item in _iter_collect_jpgs(folder):
        match = re.match(rf"^{re.escape(prefix)}_(\d+)_[12]_\d\.jpg$", item.name)
        if match:
            used.add(int(match.group(1)))
    number = 1
    while number in used:
        number += 1
    return f"{prefix}_{number}"


def collect_sample_files(root: str | Path, sample: str) -> list[Path]:
    folder = Path(root).expanduser()
    files: list[Path] = []
    files.extend(collect_json_dir(folder).glob(f"{sample}_manifest_*.json"))
    files.extend(folder.glob(f"{sample}_manifest_*.json"))
    for item in _iter_collect_jpgs(folder):
        if re.match(rf"^{re.escape(sample)}_[12]_\d\.jpg$", item.name):
            files.append(item)
    return sorted(set(files))


def list_flat_crop_names(root: str | Path | None = None, prefix: str = NEW_PREFIX) -> list[str]:
    folder = Path(root).expanduser() if root else UNSUPSET_DIR
    names: set[str] = set()
    numbered = re.compile(rf"^{re.escape(prefix)}_(\d+)$")
    for item in _iter_collect_jpgs(folder):
        match = re.match(rf"^({re.escape(prefix)}_\d+)_[12]_\d\.jpg$", item.name)
        if match:
            names.add(match.group(1))

    def key(name: str):
        match = numbered.fullmatch(name)
        return (0, int(match.group(1))) if match else (1, name)

    return sorted(names, key=key)


def is_flat_complete(root: str | Path, sample: str) -> bool:
    folder = Path(root).expanduser()
    return all(shot_path(folder, face["stage"], face["cam"], sample=sample).is_file() for face in FACES)


def list_flat_complete_names(root: str | Path, prefix: str = NEW_PREFIX) -> list[str]:
    """한 폴더의 6면이 다 있는 ok_N 이름."""
    return [name for name in list_flat_crop_names(root, prefix=prefix) if is_flat_complete(root, name)]


def next_crop_cum_index() -> int:
    """운용 수집 crop_cum_N_* 다음 번호."""
    used = set()
    if CROP_CUM_DIR.is_dir():
        for item in CROP_CUM_DIR.iterdir():
            match = CROP_CUM_FILE.fullmatch(item.name)
            if match:
                used.add(int(match.group(1)))
    number = 1
    while number in used:
        number += 1
    return number


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
