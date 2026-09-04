"""운영 UI — 검사 기록·설정 저장.

검사 1사이클(샘플 1개) = ~/darkroom_records/<판정시각>_<OK|NG>/ 폴더
  record.json   — 판정·메타
  preview.jpg   — 그리드용 미리보기
  1차/ 2차/     — 촬영본 + manifest.json
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from judgment import image_path_from_cam, load_manifest

RECORDS_ROOT = Path.home() / "darkroom_records"
HISTORY_PATH = Path.home() / "darkroom_inspection_history.json"
SETTINGS_PATH = Path.home() / "darkroom_ui_settings.json"

DEFAULT_SETTINGS = {
    "judge_backend": "stub",
    "judge_score_min": "0.25",
    "judge_model_path": "",
    "history_max": "200",
}

PREVIEW_SIZE = (360, 200)


def _load_json(path: Path, default: dict) -> dict:
    if not path.is_file():
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else dict(default)
    except (OSError, json.JSONDecodeError):
        return dict(default)


def _save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_settings() -> dict[str, str]:
    saved = _load_json(SETTINGS_PATH, DEFAULT_SETTINGS)
    out = dict(DEFAULT_SETTINGS)
    out.update({k: str(v) for k, v in saved.items() if k in DEFAULT_SETTINGS})
    if os.environ.get("JUDGE_BACKEND"):
        out["judge_backend"] = os.environ["JUDGE_BACKEND"]
    if os.environ.get("JUDGE_SCORE_MIN"):
        out["judge_score_min"] = os.environ["JUDGE_SCORE_MIN"]
    if os.environ.get("JUDGE_MODEL_PATH"):
        out["judge_model_path"] = os.environ["JUDGE_MODEL_PATH"]
    return out


def save_settings(values: dict[str, str]):
    payload = dict(DEFAULT_SETTINGS)
    payload.update({k: str(values.get(k, payload[k])) for k in DEFAULT_SETTINGS})
    _save_json(SETTINGS_PATH, payload)


def settings_env_snippet(values: dict[str, str]) -> str:
    lines = [
        f"export JUDGE_BACKEND={values.get('judge_backend', 'unsup')}",
        f"export JUDGE_SCORE_MIN={values.get('judge_score_min', '0.25')}",
    ]
    model = values.get("judge_model_path", "").strip()
    if model:
        lines.append(f"export JUDGE_MODEL_PATH={model!r}")
    return "\n".join(lines)


def apply_settings_to_env() -> dict[str, str]:
    """실행기 시작 시 설정 파일을 환경 변수로 넣는다. 이미 있는 env 는 유지."""
    if not SETTINGS_PATH.is_file():
        save_settings(DEFAULT_SETTINGS)
    values = load_settings()
    os.environ.setdefault("JUDGE_BACKEND", values.get("judge_backend") or "stub")
    os.environ.setdefault("JUDGE_SCORE_MIN", values.get("judge_score_min") or "0.25")
    model = (values.get("judge_model_path") or "").strip()
    if model:
        os.environ.setdefault("JUDGE_MODEL_PATH", model)
    return values


def _history_max() -> int:
    settings = load_settings()
    try:
        return max(10, int(settings.get("history_max", "200")))
    except ValueError:
        return 200


def _next_id() -> int:
    store = _load_json(HISTORY_PATH, {"next_id": 1})
    return int(store.get("next_id", 1))


def _bump_id(record_id: int):
    _save_json(HISTORY_PATH, {"next_id": record_id + 1})


def _parse_time(judged_at: str) -> datetime:
    if not judged_at:
        return datetime.now()
    try:
        return datetime.fromisoformat(judged_at)
    except ValueError:
        return datetime.now()


def _folder_name(judged_at: str, verdict: str, run_no: int) -> str:
    dt = _parse_time(judged_at)
    stamp = dt.strftime("%Y-%m-%d_%H-%M-%S")
    safe_verdict = verdict if verdict in ("OK", "NG") else "UNK"
    return f"{stamp}_{safe_verdict}_r{run_no:04d}"


def _unique_archive_path(judged_at: str, verdict: str, run_no: int) -> Path:
    RECORDS_ROOT.mkdir(parents=True, exist_ok=True)
    base = _folder_name(judged_at, verdict, run_no)
    path = RECORDS_ROOT / base
    if not path.exists():
        return path
    for suffix in range(2, 100):
        candidate = RECORDS_ROOT / f"{base}_{suffix}"
        if not candidate.exists():
            return candidate
    return RECORDS_ROOT / f"{base}_{int(datetime.now().timestamp())}"


def _copy_inspect_folder(src_folder: str, dest: Path, label: str) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    src = Path(src_folder) if src_folder else None
    manifest = load_manifest(str(src)) if src else None
    files: dict[int, str] = {}

    if manifest:
        for cam in manifest.get("cameras") or []:
            cam_id = int(cam.get("id", 0))
            src_file = image_path_from_cam(cam)
            if not src_file:
                continue
            dest_file = dest / f"cam{cam_id}.jpg"
            shutil.copy2(src_file, dest_file)
            files[cam_id] = str(dest_file)
    elif src and src.is_dir():
        for path in sorted(src.glob("cam*.jpg")):
            try:
                cam_id = int(path.stem.replace("cam", ""))
            except ValueError:
                continue
            dest_file = dest / path.name
            shutil.copy2(path, dest_file)
            files[cam_id] = str(dest_file)

    from camera import CAMERAS

    cameras = []
    for cam in CAMERAS:
        cameras.append(
            {
                "id": cam["id"],
                "name": cam["name"],
                "device": cam.get("device"),
                "file": files.get(cam["id"]),
            }
        )
    payload = {
        "label": label,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "folder": str(dest),
        "cameras": cameras,
    }
    (dest / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(dest)


def _pick_preview_source(judgment: dict[str, Any]) -> tuple[str, list[list[int]]]:
    defects = judgment.get("defects") or []
    if defects:
        item = defects[0]
        cam_id = int(item.get("cam_id", 1))
        inspect = item.get("inspect", "1차")
        folder = judgment.get("manifest_1") if inspect == "1차" else judgment.get("manifest_2")
        manifest = load_manifest(folder or "")
        bboxes = [
            d.get("bbox")
            for d in defects
            if d.get("inspect") == inspect and int(d.get("cam_id", 0)) == cam_id
        ]
        if manifest:
            for cam in manifest.get("cameras") or []:
                path = image_path_from_cam(cam)
                if int(cam.get("id", 0)) == cam_id and path:
                    return path, [b for b in bboxes if b]
    for folder_key in ("manifest_1", "manifest_2"):
        manifest = load_manifest(judgment.get(folder_key) or "")
        if not manifest:
            continue
        for cam in manifest.get("cameras") or []:
            path = image_path_from_cam(cam)
            if path:
                return str(path), []
    return "", []


def _make_preview(archive: Path, judgment: dict[str, Any]) -> Path:
    from PIL import Image, ImageDraw

    preview_path = archive / "preview.jpg"
    src, bboxes = _pick_preview_source(judgment)
    if not src or not Path(src).is_file():
        img = Image.new("RGB", PREVIEW_SIZE, color=(15, 22, 40))
        draw = ImageDraw.Draw(img)
        draw.text((12, 12), "미리보기 없음", fill=(148, 163, 184))
        img.save(preview_path, quality=85)
        return preview_path

    img = Image.open(src).convert("RGB")
    orig_w, orig_h = img.size
    img.thumbnail(PREVIEW_SIZE)
    if bboxes and orig_w > 0 and orig_h > 0:
        sx, sy = img.size[0] / orig_w, img.size[1] / orig_h
        draw = ImageDraw.Draw(img)
        for idx, bbox in enumerate(bboxes):
            if not bbox or len(bbox) < 4:
                continue
            x1, y1, x2, y2 = bbox[:4]
            box = [x1 * sx, y1 * sy, x2 * sx, y2 * sy]
            draw.rectangle(box, outline="#EF4444", width=3)
    img.save(preview_path, quality=85)
    return preview_path


def load_record_from_archive(archive_path: str | Path) -> dict[str, Any] | None:
    folder = Path(archive_path)
    record_file = folder / "record.json"
    if not record_file.is_file():
        return None
    try:
        return json.loads(record_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not RECORDS_ROOT.is_dir():
        return records
    for folder in RECORDS_ROOT.iterdir():
        if not folder.is_dir():
            continue
        rec = load_record_from_archive(folder)
        if rec:
            records.append(rec)
    records.sort(key=lambda r: r.get("judged_at", ""), reverse=True)
    return records


def append_record(judgment: dict[str, Any], run_no: int) -> dict[str, Any]:
    judged_at = judgment.get("judged_at") or datetime.now().isoformat(timespec="seconds")
    verdict = judgment.get("verdict", "—")
    record_id = _next_id()
    archive = _unique_archive_path(judged_at, verdict, run_no)
    archive.mkdir(parents=True, exist_ok=True)

    inspect_1 = _copy_inspect_folder(judgment.get("manifest_1", ""), archive / "1차", "1차")
    inspect_2 = _copy_inspect_folder(judgment.get("manifest_2", ""), archive / "2차", "2차")

    archived_judgment = dict(judgment)
    archived_judgment["manifest_1"] = inspect_1
    archived_judgment["manifest_2"] = inspect_2
    preview = _make_preview(archive, archived_judgment)

    record = {
        "id": record_id,
        "run_no": run_no,
        "verdict": verdict,
        "defect_count": len(judgment.get("defects") or []),
        "backend": judgment.get("backend", ""),
        "judged_at": judged_at,
        "archive_path": str(archive),
        "preview": str(preview),
        "message": judgment.get("message", ""),
        "judgment": archived_judgment,
    }
    (archive / "record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _bump_id(record_id)
    _trim_old_records()
    return record


def _trim_old_records():
    records = list_records()
    limit = _history_max()
    for rec in records[limit:]:
        path = rec.get("archive_path")
        if path and Path(path).is_dir():
            shutil.rmtree(path, ignore_errors=True)


def clear_history():
    if RECORDS_ROOT.is_dir():
        for folder in RECORDS_ROOT.iterdir():
            if folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)
    _save_json(HISTORY_PATH, {"next_id": 1})


def history_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    ok = sum(1 for r in records if r.get("verdict") == "OK")
    ng = sum(1 for r in records if r.get("verdict") == "NG")
    rate = f"{ok / total * 100:.1f}%" if total else "—"
    return {"total": total, "ok": ok, "ng": ng, "rate": rate}


def format_judged_at(judged_at: str) -> str:
    dt = _parse_time(judged_at)
    return dt.strftime("%Y-%m-%d  %H:%M:%S")


def load_history() -> dict[str, Any]:
    """레거시 호환 — 폴더 기록 목록."""
    records = list_records()
    return {"records": records, "next_id": _next_id()}


_DEMO_MARKER = "demo"
_DEMO_FACE_IDS = {
    "1차": (1, 2, 3, 4),
    "2차": (3, 4),
}
_FACE_PALETTE = (
    ((18, 32, 52), (56, 189, 248)),
    ((22, 38, 36), (52, 211, 153)),
    ((36, 28, 48), (167, 139, 250)),
    ((40, 32, 24), (251, 191, 36)),
    ((28, 24, 44), (244, 114, 182)),
    ((16, 36, 40), (45, 212, 191)),
)


def _synthetic_face_image(
    face_id: int,
    inspect_label: str,
    bboxes: list[list[int]] | None = None,
):
    from PIL import Image, ImageDraw

    w, h = 1280, 720
    idx = max(0, min(face_id - 1, len(_FACE_PALETTE) - 1))
    bg, accent = _FACE_PALETTE[idx]
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)

    for x in range(0, w, 48):
        draw.line([(x, 0), (x, h)], fill=(bg[0] + 8, bg[1] + 8, bg[2] + 12), width=1)
    for y in range(0, h, 48):
        draw.line([(0, y), (w, y)], fill=(bg[0] + 8, bg[1] + 8, bg[2] + 12), width=1)

    cx, cy = w // 2, h // 2
    draw.rectangle([cx - 220, cy - 140, cx + 220, cy + 140], outline=accent, width=3)
    draw.rectangle([cx - 180, cy - 100, cx - 60, cy + 20], fill=(bg[0] + 20, bg[1] + 20, bg[2] + 24))
    draw.rectangle([cx - 20, cy - 100, cx + 100, cy + 20], fill=(bg[0] + 24, bg[1] + 24, bg[2] + 28))
    draw.rectangle([cx + 60, cy - 100, cx + 180, cy + 20], fill=(bg[0] + 16, bg[1] + 16, bg[2] + 20))
    draw.ellipse([cx - 30, cy + 40, cx + 30, cy + 100], outline=accent, width=2)

    draw.text((28, 24), f"{inspect_label}  ·  면 {face_id}", fill=(148, 163, 184))
    draw.text((28, 54), f"SAMPLE #{face_id:02d}", fill=accent)
    draw.text((cx - 18, cy - 18), str(face_id), fill=accent)

    for bbox in bboxes or []:
        if not bbox or len(bbox) < 4:
            continue
        x1, y1, x2, y2 = bbox[:4]
        draw.rectangle([x1, y1, x2, y2], outline=(251, 113, 133), width=4)
        draw.rectangle([x1 + 2, y1 + 2, x2 - 2, y2 - 2], outline=(254, 205, 211), width=1)
    return img


def _write_demo_inspect_folder(
    dest: Path,
    label: str,
    face_ids: tuple[int, ...],
    defect_map: dict[int, list[list[int]]] | None = None,
) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    defect_map = defect_map or {}
    cameras = []
    for face_id in face_ids:
        img = _synthetic_face_image(face_id, label, defect_map.get(face_id))
        out = dest / f"cam{face_id}.jpg"
        img.save(out, quality=90)
        cameras.append(
            {
                "id": face_id,
                "name": f"면 {face_id}",
                "device": None,
                "file": str(out),
            }
        )
    payload = {
        "label": label,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "folder": str(dest),
        "face_count": len(face_ids),
        "cameras": cameras,
    }
    (dest / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(dest)


def _demo_judgment(
    verdict: str,
    judged_at: str,
    run_no: int,
    message: str,
    defects: list[dict[str, Any]],
    staging: Path,
    tag: str,
) -> dict[str, Any]:
    defect_map_1: dict[int, list[list[int]]] = {}
    defect_map_2: dict[int, list[list[int]]] = {}
    for item in defects:
        face_id = int(item["cam_id"])
        bbox = item.get("bbox")
        if not bbox:
            continue
        bucket = defect_map_1 if item.get("inspect") == "1차" else defect_map_2
        bucket.setdefault(face_id, []).append(bbox)

    return {
        "verdict": verdict,
        "defects": defects,
        "backend": _DEMO_MARKER,
        "message": message,
        "judged_at": judged_at,
        "manifest_1": _write_demo_inspect_folder(
            staging / f"{tag}_1차",
            "1차",
            _DEMO_FACE_IDS["1차"],
            defect_map_1,
        ),
        "manifest_2": _write_demo_inspect_folder(
            staging / f"{tag}_2차",
            "2차",
            _DEMO_FACE_IDS["2차"],
            defect_map_2,
        ),
        "_run_no": run_no,
    }


_DEMO_SAMPLES = (
    {
        "tag": "ok_a",
        "verdict": "OK",
        "run_no": 1,
        "minutes_ago": 58,
        "message": "데모 — 정상 샘플 A",
        "defects": [],
    },
    {
        "tag": "ok_b",
        "verdict": "OK",
        "run_no": 2,
        "minutes_ago": 47,
        "message": "데모 — 정상 샘플 B",
        "defects": [],
    },
    {
        "tag": "ok_c",
        "verdict": "OK",
        "run_no": 3,
        "minutes_ago": 36,
        "message": "데모 — 정상 샘플 C",
        "defects": [],
    },
    {
        "tag": "ng_scratch",
        "verdict": "NG",
        "run_no": 4,
        "minutes_ago": 24,
        "message": "데모 — 1차 면 2 스크래치",
        "defects": [
            {
                "cam_id": 2,
                "inspect": "1차",
                "class_name": "scratch",
                "score": 0.87,
                "bbox": [430, 250, 590, 390],
            },
        ],
    },
    {
        "tag": "ng_flip",
        "verdict": "NG",
        "run_no": 5,
        "minutes_ago": 14,
        "message": "데모 — 2차 면 5·6 복합 불량",
        "defects": [
            {
                "cam_id": 3,
                "inspect": "2차",
                "class_name": "contamination",
                "score": 0.81,
                "bbox": [360, 210, 520, 350],
            },
            {
                "cam_id": 4,
                "inspect": "2차",
                "class_name": "dent",
                "score": 0.93,
                "bbox": [700, 260, 860, 400],
            },
        ],
    },
    {
        "tag": "ng_edge",
        "verdict": "NG",
        "run_no": 6,
        "minutes_ago": 5,
        "message": "데모 — 1차 면 3 모서리 깨짐",
        "defects": [
            {
                "cam_id": 3,
                "inspect": "1차",
                "class_name": "edge_break",
                "score": 0.89,
                "bbox": [820, 180, 980, 340],
            },
        ],
    },
)


def remove_demo_records():
    removed = 0
    for rec in list_records():
        if rec.get("backend") != _DEMO_MARKER:
            continue
        path = rec.get("archive_path")
        if path and Path(path).is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    staging = RECORDS_ROOT / "_demo_staging"
    if staging.is_dir():
        shutil.rmtree(staging, ignore_errors=True)
    return removed


def seed_demo_records(force: bool = False) -> list[dict[str, Any]]:
    """데모 6건 — 개발용 CLI. 운영 UI는 쓰지 않는다."""
    from datetime import timedelta

    existing = [r for r in list_records() if r.get("backend") == _DEMO_MARKER]
    if not force and len(existing) >= len(_DEMO_SAMPLES):
        return existing

    remove_demo_records()
    staging = RECORDS_ROOT / "_demo_staging"
    if staging.is_dir():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    created: list[dict[str, Any]] = []
    for sample in _DEMO_SAMPLES:
        judged_at = (now - timedelta(minutes=sample["minutes_ago"])).isoformat(timespec="seconds")
        judgment = _demo_judgment(
            verdict=sample["verdict"],
            judged_at=judged_at,
            run_no=sample["run_no"],
            message=sample["message"],
            defects=list(sample["defects"]),
            staging=staging,
            tag=sample["tag"],
        )
        run_no = judgment.pop("_run_no")
        created.append(append_record(judgment, run_no=run_no))

    shutil.rmtree(staging, ignore_errors=True)
    return created


if __name__ == "__main__":
    import sys

    force = "--force" in sys.argv
    records = seed_demo_records(force=force)
    print(f"[demo] {len(records)}건 생성 → {RECORDS_ROOT}")
    for rec in records:
        print(f"  {rec.get('verdict')}  {rec.get('archive_path')}")

