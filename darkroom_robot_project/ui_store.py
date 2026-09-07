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

from judgment import image_path_from_cam, load_manifest, merge_unsup_yolo_boxes

RECORDS_ROOT = Path.home() / "darkroom_records"
HISTORY_PATH = Path.home() / "darkroom_inspection_history.json"
STATS_DIR = Path.home() / "darkroom_report_stats"
STATS_PATH = STATS_DIR
LABELED_ROOT = Path.home() / "darkroom_labeled"
LEGACY_STATS_PATH = Path.home() / "darkroom_report_stats.jsonl"
SETTINGS_PATH = Path.home() / "darkroom_ui_settings.json"

DEFAULT_SETTINGS = {
    "judge_backend": "unsup",
    "judge_score_min": "0.60",
    "judge_model_path": "",
    "history_max": "200",
}

PREVIEW_SIZE = (360, 200)
LABELED_SIZE = (640, 360)
_LABELED_FACES = (
    ("1차", 1, "면1"),
    ("1차", 2, "면2"),
    ("1차", 3, "면3"),
    ("1차", 4, "면4"),
    ("2차", 3, "면5"),
    ("2차", 4, "면6"),
)


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
    return out


def save_settings(values: dict[str, str]):
    payload = dict(DEFAULT_SETTINGS)
    payload.update({k: str(values.get(k, payload[k])) for k in DEFAULT_SETTINGS})
    _save_json(SETTINGS_PATH, payload)


def settings_env_snippet(values: dict[str, str]) -> str:
    lines = [
        f"export JUDGE_BACKEND={values.get('judge_backend', 'unsup')}",
        f"export JUDGE_SCORE_MIN={values.get('judge_score_min', '0.60')}",
    ]
    model = values.get("judge_model_path", "").strip()
    if model:
        lines.append(f"export JUDGE_MODEL_PATH={model!r}")
    return "\n".join(lines)


def apply_settings_to_env() -> dict[str, str]:
    """실행기 시작 시 설정 파일을 환경 변수로 넣는다."""
    if not SETTINGS_PATH.is_file():
        save_settings(DEFAULT_SETTINGS)
    values = load_settings()
    os.environ["JUDGE_BACKEND"] = values.get("judge_backend") or "unsup"
    os.environ["JUDGE_SCORE_MIN"] = values.get("judge_score_min") or "0.60"
    model = (values.get("judge_model_path") or "").strip()
    if model:
        os.environ["JUDGE_MODEL_PATH"] = model
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


def preview_source_and_boxes(judgment: dict[str, Any]) -> tuple[str, list[list[int]], int, str]:
    """전체 화면 미리보기용 촬영본과 bbox. 크롭 좌표는 쓰지 않는다."""
    defects = judgment.get("defects") or []
    if defects:
        item = defects[0]
        cam_id = int(item.get("cam_id", 1))
        inspect = item.get("inspect", "1차")
        folder = judgment.get("manifest_1") if inspect == "1차" else judgment.get("manifest_2")
        manifest = load_manifest(folder or "")
        face_items = [
            d
            for d in defects
            if d.get("inspect") == inspect and int(d.get("cam_id", 0)) == cam_id
        ]
        bboxes = merge_unsup_yolo_boxes(face_items, use_crop=False)
        if manifest:
            for cam in manifest.get("cameras") or []:
                path = image_path_from_cam(cam)
                if int(cam.get("id", 0)) == cam_id and path:
                    return path, bboxes, cam_id, inspect
    inspect = "1차"
    for folder_key, label in (("manifest_1", "1차"), ("manifest_2", "2차")):
        manifest = load_manifest(judgment.get(folder_key) or "")
        if not manifest:
            continue
        for cam in manifest.get("cameras") or []:
            path = image_path_from_cam(cam)
            if path:
                return str(path), [], int(cam.get("id", 0) or 0), label
    return "", [], 0, "1차"


def _pick_preview_source(judgment: dict[str, Any]) -> tuple[str, list[list[int]], int, str]:
    return preview_source_and_boxes(judgment)


def _make_preview(archive: Path, judgment: dict[str, Any]) -> Path:
    from PIL import Image, ImageDraw

    preview_path = archive / "preview.jpg"
    src, bboxes, cam_id, inspect = _pick_preview_source(judgment)
    if not src or not Path(src).is_file():
        img = Image.new("RGB", PREVIEW_SIZE, color=(15, 22, 40))
        draw = ImageDraw.Draw(img)
        draw.text((12, 12), "미리보기 없음", fill=(148, 163, 184))
        img.save(preview_path, quality=85)
        return preview_path

    if cam_id:
        from camera_calib import inspect_stage, open_camera_rgb

        img = open_camera_rgb(src, int(cam_id), stage=inspect_stage(inspect))
    else:
        img = Image.open(src).convert("RGB")
    overlay = list(bboxes)
    orig_w, orig_h = img.size
    img.thumbnail(PREVIEW_SIZE, Image.BILINEAR)
    if overlay and orig_w > 0 and orig_h > 0:
        sx, sy = img.size[0] / orig_w, img.size[1] / orig_h
        draw = ImageDraw.Draw(img)
        for bbox in overlay:
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


def _compact_record(record: dict[str, Any]) -> dict[str, Any]:
    """리포트용 최소 수치. 사진·전체 판정 JSON은 넣지 않는다."""
    judgment = record.get("judgment") or {}
    defects = []
    for item in judgment.get("defects") or []:
        score = item.get("score")
        try:
            score = round(float(score), 4) if score is not None else None
        except (TypeError, ValueError):
            score = None
        defects.append(
            {
                "class_name": str(item.get("class_name") or ""),
                "score": score,
                "cam_id": item.get("cam_id"),
                "inspect": item.get("inspect"),
            }
        )
    return {
        "id": record.get("id"),
        "run_no": record.get("run_no"),
        "verdict": record.get("verdict"),
        "judged_at": record.get("judged_at"),
        "backend": record.get("backend", ""),
        "defect_count": int(record.get("defect_count") or len(defects)),
        "message": record.get("message") or "",
        "preview": record.get("preview") or "",
        "labeled_dir": record.get("labeled_dir") or "",
        "faces": list(record.get("faces") or []),
        "judgment": {"verdict": record.get("verdict"), "defects": defects},
    }


def report_day(judged_at: str | None = None) -> str:
    """리포트 파일 날짜. 비우면 오늘(로컬)."""
    dt = _parse_time(judged_at) if judged_at else datetime.now()
    return dt.strftime("%Y-%m-%d")


def stats_path_for_day(day: str | None = None) -> Path:
    return STATS_DIR / f"{day or report_day()}.jsonl"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _migrate_legacy_stats():
    """예전 단일 jsonl 을 날짜별 파일로 나눈다. 한 번만."""
    if not LEGACY_STATS_PATH.is_file():
        return
    STATS_DIR.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list[str]] = {}
    for rec in _read_jsonl(LEGACY_STATS_PATH):
        day = report_day(rec.get("judged_at"))
        line = json.dumps(_compact_record(rec), ensure_ascii=False, separators=(",", ":"))
        by_day.setdefault(day, []).append(line)
    for day, lines in by_day.items():
        dest = stats_path_for_day(day)
        with dest.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
            fh.write("\n")
    bak = LEGACY_STATS_PATH.with_name(LEGACY_STATS_PATH.name + ".bak")
    try:
        LEGACY_STATS_PATH.replace(bak)
    except OSError:
        pass


def list_report_days() -> list[str]:
    _migrate_legacy_stats()
    days = set()
    if STATS_DIR.is_dir():
        for path in STATS_DIR.glob("????-??-??.jsonl"):
            try:
                if path.stat().st_size > 0:
                    days.add(path.stem)
            except OSError:
                continue
    days.add(report_day())
    return sorted(days)


def _stats_ids() -> set:
    _migrate_legacy_stats()
    ids = set()
    if not STATS_DIR.is_dir():
        return ids
    for path in STATS_DIR.glob("*.jsonl"):
        for row in _read_jsonl(path):
            rec_id = row.get("id")
            if rec_id is not None:
                ids.add(rec_id)
    return ids


def append_stats_row(record: dict[str, Any]):
    _migrate_legacy_stats()
    STATS_DIR.mkdir(parents=True, exist_ok=True)
    dest = stats_path_for_day(report_day(record.get("judged_at")))
    line = json.dumps(_compact_record(record), ensure_ascii=False, separators=(",", ":"))
    with dest.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.write("\n")


def list_stats_records(day: str | None = None) -> list[dict[str, Any]]:
    _migrate_legacy_stats()
    return _read_jsonl(stats_path_for_day(day or report_day()))


def list_report_records(day: str | None = None) -> list[dict[str, Any]]:
    """지정한 날의 리포트. 기본은 오늘. 세션 폴더가 있으면 미리보기 있는 쪽을 쓴다."""
    day = day or report_day()
    by_id: dict[Any, dict[str, Any]] = {}
    for rec in list_stats_records(day):
        rec_id = rec.get("id")
        if rec_id is not None:
            by_id[rec_id] = rec
        else:
            by_id[id(rec)] = rec
    for rec in list_records():
        if report_day(rec.get("judged_at")) != day:
            continue
        rec_id = rec.get("id")
        if rec_id is not None:
            by_id[rec_id] = rec
        else:
            by_id[id(rec)] = rec
    for rec in list_labeled_records(day):
        rec_id = rec.get("id")
        if rec_id is not None:
            by_id[rec_id] = rec
        else:
            by_id[id(rec)] = rec
    records = list(by_id.values())
    records.sort(key=lambda r: r.get("judged_at", ""), reverse=True)
    return records


def labeled_dir_for(record: dict[str, Any]) -> Path:
    day = report_day(record.get("judged_at"))
    rec_id = record.get("id") or "x"
    verdict = record.get("verdict") or "UNK"
    return LABELED_ROOT / day / f"{rec_id}_{verdict}"


def _face_source_path(record: dict[str, Any], inspect: str, cam_id: int) -> str:
    judgment = record.get("judgment") or {}
    folder = judgment.get("manifest_1") if inspect == "1차" else judgment.get("manifest_2")
    manifest = load_manifest(folder or "")
    if not manifest:
        return ""
    for cam in manifest.get("cameras") or []:
        if int(cam.get("id", 0)) == int(cam_id):
            return image_path_from_cam(cam) or ""
    return ""


def _draw_boxes(img, bboxes, orig_size):
    if not bboxes:
        return
    orig_w, orig_h = orig_size
    w, h = img.size
    if orig_w <= 0 or orig_h <= 0:
        return
    sx, sy = w / orig_w, h / orig_h
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    for bbox in bboxes:
        if not bbox or len(bbox) < 4:
            continue
        x1, y1, x2, y2 = bbox[:4]
        draw.rectangle([x1 * sx, y1 * sy, x2 * sx, y2 * sy], outline="#EF4444", width=3)


def save_labeled_archive(record: dict[str, Any]) -> dict[str, Any]:
    """원본 6장을 결함 박스가 그려진 JPEG로 줄여 보관한다."""
    from PIL import Image

    dest = labeled_dir_for(record)
    dest.mkdir(parents=True, exist_ok=True)
    judgment = record.get("judgment") or {}
    defects = judgment.get("defects") or []
    faces = []
    preview_src = ""
    for inspect, cam_id, name in _LABELED_FACES:
        src = _face_source_path(record, inspect, cam_id)
        if not src or not Path(src).is_file():
            continue
        boxes = [
            item.get("bbox")
            for item in defects
            if item.get("inspect") == inspect and int(item.get("cam_id", 0)) == cam_id and item.get("bbox")
        ]
        img = Image.open(src).convert("RGB")
        orig = img.size
        img.thumbnail(LABELED_SIZE, Image.BILINEAR)
        _draw_boxes(img, boxes, orig)
        out = dest / f"{inspect}_cam{cam_id}.jpg"
        img.save(out, quality=82, optimize=True)
        faces.append(
            {
                "inspect": inspect,
                "cam_id": cam_id,
                "name": name,
                "file": str(out),
            }
        )
        if boxes and not preview_src:
            preview_src = str(out)
        elif not preview_src:
            preview_src = str(out)
    preview = dest / "preview.jpg"
    if preview_src and Path(preview_src).is_file():
        if Path(preview_src).resolve() != preview.resolve():
            shutil.copy2(preview_src, preview)
        else:
            preview = Path(preview_src)
    compact = _compact_record(record)
    compact["preview"] = str(preview) if preview.is_file() else ""
    compact["labeled_dir"] = str(dest)
    compact["faces"] = faces
    compact["judgment"] = {
        "verdict": record.get("verdict"),
        "defects": compact["judgment"]["defects"],
        "backend": record.get("backend", ""),
        "message": record.get("message", ""),
    }
    (dest / "meta.json").write_text(
        json.dumps(compact, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return compact


def list_labeled_days() -> list[str]:
    days = []
    if not LABELED_ROOT.is_dir():
        return days
    for path in sorted(LABELED_ROOT.iterdir()):
        if path.is_dir() and any(path.glob("*/meta.json")):
            days.append(path.name)
    return days


def list_labeled_records(day: str | None = None) -> list[dict[str, Any]]:
    day = day or report_day()
    folder = LABELED_ROOT / day
    records: list[dict[str, Any]] = []
    if not folder.is_dir():
        return records
    for item in folder.iterdir():
        meta = item / "meta.json" if item.is_dir() else None
        if not meta or not meta.is_file():
            continue
        try:
            rec = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(rec, dict):
            records.append(rec)
    records.sort(key=lambda r: r.get("judged_at", ""), reverse=True)
    return records


def purge_record_files():
    """촬영 폴더만 지운다. 리포트 jsonl·next_id 는 유지."""
    if not RECORDS_ROOT.is_dir():
        return
    for folder in RECORDS_ROOT.iterdir():
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)


def flush_archives_to_stats():
    """종료 시 결함 박스 JPEG만 남기고 원본 기록 폴더는 지운다."""
    known = _stats_ids()
    for rec in list_records():
        try:
            packed = save_labeled_archive(rec)
        except Exception as exc:
            print(f"[기록] 라벨 보관 실패 id={rec.get('id')}: {exc}")
            packed = rec
        rec_id = packed.get("id")
        if rec_id not in known:
            append_stats_row(packed)
            if rec_id is not None:
                known.add(rec_id)
    purge_record_files()


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
    known = _stats_ids()
    for rec in records[limit:]:
        rec_id = rec.get("id")
        try:
            packed = save_labeled_archive(rec)
        except Exception:
            packed = rec
        if rec_id not in known:
            append_stats_row(packed)
            if rec_id is not None:
                known.add(rec_id)
        path = rec.get("archive_path")
        if path and Path(path).is_dir():
            shutil.rmtree(path, ignore_errors=True)


def clear_history():
    """이번 세션 이미지 폴더만 지운다. 리포트 누적 수치는 유지."""
    purge_record_files()


def delete_records(record_ids) -> int:
    """선택한 기록 폴더만 지운다. 지운 개수를 반환."""
    wanted = {int(x) for x in record_ids}
    removed = 0
    for rec in list_records():
        try:
            rec_id = int(rec.get("id"))
        except (TypeError, ValueError):
            continue
        if rec_id not in wanted:
            continue
        path = rec.get("archive_path")
        if path and Path(path).is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed


def _xlsx_cell(value: Any) -> str:
    from xml.sax.saxutils import escape

    if value is None:
        return '<c/>'
    if isinstance(value, bool):
        return f'<c t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c><v>{value}</v></c>'
    text = escape(str(value))
    return f'<c t="inlineStr"><is><t>{text}</t></is></c>'


def write_xlsx(path: str | Path, sheets: dict[str, list[list[Any]]]) -> Path:
    """openpyxl 없이 최소 xlsx를 만든다."""
    import zipfile

    dest = Path(path).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    names = list(sheets.keys()) or ["Sheet1"]
    workbook_sheets = []
    sheet_files = {}
    for idx, name in enumerate(names, start=1):
        rows = sheets.get(name) or []
        xml_rows = []
        for r_i, row in enumerate(rows, start=1):
            cells = "".join(_xlsx_cell(col) for col in row)
            xml_rows.append(f'<row r="{r_i}">{cells}</row>')
        sheet_files[f"xl/worksheets/sheet{idx}.xml"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
        )
        safe = name.replace("&", "")[:31] or f"Sheet{idx}"
        workbook_sheets.append(
            f'<sheet name="{safe}" sheetId="{idx}" r:id="rId{idx}"/>'
        )
    content_types = [
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
    ]
    rels = []
    for idx in range(1, len(names) + 1):
        content_types.append(
            f'<Override PartName="/xl/worksheets/sheet{idx}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
        rels.append(
            f'<Relationship Id="rId{idx}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{idx}.xml"/>'
        )
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            + "".join(content_types)
            + "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets>{"".join(workbook_sheets)}</sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(rels)
            + "</Relationships>"
        ),
        "xl/styles.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<fonts count=\"1\"><font><sz val=\"11\"/><name val=\"Calibri\"/></font></fonts>"
            "<fills count=\"1\"><fill><patternFill patternType=\"none\"/></fill></fills>"
            "<borders count=\"1\"><border/></borders>"
            "<cellStyleXfs count=\"1\"><xf/></cellStyleXfs>"
            "<cellXfs count=\"1\"><xf/></cellXfs>"
            "</styleSheet>"
        ),
        **sheet_files,
    }
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, payload in files.items():
            zf.writestr(name, payload.encode("utf-8"))
    return dest


_FACE_EXPORT = {
    ("1차", 1): "면1",
    ("1차", 2): "면2",
    ("1차", 3): "면3",
    ("1차", 4): "면4",
    ("2차", 3): "면5",
    ("2차", 4): "면6",
}


def export_ng_xlsx(path: str | Path, records: list[dict[str, Any]] | None = None) -> Path:
    """NG 기록과 결함 목록을 엑셀로 보낸다."""
    from reports_analytics import defect_label

    rows = records if records is not None else list_report_records()
    ng_rows = [r for r in rows if str(r.get("verdict", "")).upper() == "NG"]
    summary = [
        ["기록ID", "시각", "Run", "폴더", "결함수", "유형", "백엔드", "메시지", "경로"],
    ]
    detail = [
        ["기록ID", "시각", "Run", "면", "검사", "카메라", "유형", "점수", "문턱", "x1", "y1", "x2", "y2", "크롭x1", "크롭y1", "크롭x2", "크롭y2", "메시지"],
    ]
    for rec in ng_rows:
        judgment = rec.get("judgment") or {}
        defects = judgment.get("defects") or []
        types = ", ".join(
            defect_label(str(d.get("class_name", ""))) for d in defects
        ) or "—"
        rec_id = rec.get("id", "")
        judged = rec.get("judged_at", "")
        run_no = rec.get("run_no", "")
        folder = Path(rec.get("archive_path", "")).name
        summary.append([
            rec_id,
            judged,
            run_no,
            folder,
            len(defects),
            types,
            rec.get("backend", ""),
            rec.get("message", ""),
            rec.get("archive_path", ""),
        ])
        if not defects:
            detail.append([
                rec_id, judged, run_no, "", "", "", "검출 없음", "", "", "", "", "", "", "", "", "", "", rec.get("message", ""),
            ])
            continue
        for defect in defects:
            inspect = str(defect.get("inspect", ""))
            cam_id = int(defect.get("cam_id") or 0)
            bbox = defect.get("bbox") or [None, None, None, None]
            crop = defect.get("crop_bbox") or [None, None, None, None]
            bbox = list(bbox) + [None] * 4
            crop = list(crop) + [None] * 4
            score = defect.get("score")
            threshold = defect.get("threshold")
            detail.append([
                rec_id,
                judged,
                run_no,
                _FACE_EXPORT.get((inspect, cam_id), ""),
                inspect,
                cam_id,
                defect_label(str(defect.get("class_name", ""))),
                round(float(score), 4) if score is not None else "",
                round(float(threshold), 4) if threshold not in (None, "") else "",
                bbox[0], bbox[1], bbox[2], bbox[3],
                crop[0], crop[1], crop[2], crop[3],
                rec.get("message", ""),
            ])
    return write_xlsx(
        path,
        {"NG기록": summary, "결함상세": detail},
    )


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

