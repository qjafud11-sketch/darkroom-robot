"""판정 — 1·2차 manifest → verdict + defects.

파이프라인·UI는 이 모듈의 결과 형식(JSON)만 보면 된다.

환경 변수:
  JUDGE_BACKEND   unsup(기본, 비지도+YOLO) | yolo | both | stub | mock | model
  JUDGE_MODEL_PATH   결함 모델. 비우면 ~/darkroom_models/openvino/defect_det/model.xml
                     (없으면 yolo/crop640/weights/best.pt)
  JUDGE_SCORE_MIN    YOLO NG confidence (기본 0.60). 비지도는 면별 threshold.json
  JUDGE_MOCK_NG      mock 백엔드에서 1이면 테스트 NG
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

RESULT_PATH = Path.home() / "darkroom_last_judgment.json"

_last_result: dict[str, Any] | None = None


@dataclass
class Defect:
    cam_id: int
    inspect: str  # "1차" | "2차"
    class_name: str
    score: float
    bbox: list[int]  # [x1, y1, x2, y2] — 촬영 프레임 기준
    threshold: float = 0.0
    crop_bbox: list[int] = field(default_factory=list)  # 샘플 면 크롭 좌표

    def to_dict(self):
        return asdict(self)


@dataclass
class JudgmentResult:
    verdict: str  # OK | NG
    defects: list[Defect] = field(default_factory=list)
    scores: list[dict[str, Any]] = field(default_factory=list)
    manifest_1: str = ""
    manifest_2: str = ""
    backend: str = "unsup"
    message: str = ""
    judged_at: str = ""

    def to_dict(self):
        data = asdict(self)
        data["defects"] = [d.to_dict() if isinstance(d, Defect) else d for d in self.defects]
        return data


_YOLO_CLASSES = {"scratch", "dent"}
_BOX_MERGE_IOU = 0.25
_BOX_MERGE_IOS = 0.45


def _as_box(raw) -> list[float] | None:
    if not raw or len(raw) < 4:
        return None
    x1, y1, x2, y2 = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
    if x2 <= x1 or y2 <= y1:
        return None
    return [x1, y1, x2, y2]


def _defect_source(item: dict[str, Any]) -> str:
    src = str(item.get("source") or "").strip().lower()
    if src:
        return src
    key = str(item.get("class_name") or "").strip().lower()
    return "yolo" if key in _YOLO_CLASSES else "unsup"


def box_iou(a, b) -> float:
    aa, bb = _as_box(a), _as_box(b)
    if not aa or not bb:
        return 0.0
    x1, y1 = max(aa[0], bb[0]), max(aa[1], bb[1])
    x2, y2 = min(aa[2], bb[2]), min(aa[3], bb[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0:
        return 0.0
    area_a = (aa[2] - aa[0]) * (aa[3] - aa[1])
    area_b = (bb[2] - bb[0]) * (bb[3] - bb[1])
    union = area_a + area_b - inter
    return inter / union if union else 0.0


def box_ios(a, b) -> float:
    """교집합 / 더 작은 박스. YOLO가 비지도 영역 안에 들어 있는 경우를 잡는다."""
    aa, bb = _as_box(a), _as_box(b)
    if not aa or not bb:
        return 0.0
    x1, y1 = max(aa[0], bb[0]), max(aa[1], bb[1])
    x2, y2 = min(aa[2], bb[2]), min(aa[3], bb[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0:
        return 0.0
    smaller = min((aa[2] - aa[0]) * (aa[3] - aa[1]), (bb[2] - bb[0]) * (bb[3] - bb[1]))
    return inter / smaller if smaller else 0.0


def boxes_same_region(a, b) -> bool:
    return box_iou(a, b) >= _BOX_MERGE_IOU or box_ios(a, b) >= _BOX_MERGE_IOS


def box_union(a, b) -> list[float]:
    aa, bb = _as_box(a), _as_box(b)
    if not aa:
        return list(bb or [])
    if not bb:
        return list(aa)
    return [min(aa[0], bb[0]), min(aa[1], bb[1]), max(aa[2], bb[2]), max(aa[3], bb[3])]


def merge_unsup_yolo_boxes(items: list[dict[str, Any]], *, use_crop: bool = False) -> list[list[int]]:
    """같은 면의 비지도·YOLO 박스가 같은 영역이면 합집합 하나."""
    unsup: list[list[float]] = []
    yolo: list[list[float]] = []
    other: list[list[float]] = []
    for item in items:
        raw = item.get("crop_bbox") if use_crop else item.get("bbox")
        box = _as_box(raw)
        if not box:
            continue
        src = _defect_source(item)
        if src == "yolo":
            yolo.append(box)
        elif src == "unsup":
            unsup.append(box)
        else:
            other.append(box)
    used = [False] * len(yolo)
    out: list[list[int]] = []
    for u in unsup:
        acc = list(u)
        changed = True
        while changed:
            changed = False
            for i, y in enumerate(yolo):
                if used[i]:
                    continue
                if boxes_same_region(acc, y):
                    acc = box_union(acc, y)
                    used[i] = True
                    changed = True
        out.append([int(round(v)) for v in acc])
    for i, y in enumerate(yolo):
        if not used[i]:
            out.append([int(round(v)) for v in y])
    out.extend([int(round(v)) for v in box] for box in other)
    return out


def overlay_highlight_index(merged: list[list[int]], target) -> int:
    if not merged:
        return -1
    box = _as_box(target)
    if not box:
        return 0
    best_i, best = 0, -1.0
    for i, other in enumerate(merged):
        score = max(box_iou(box, other), box_ios(box, other))
        if score > best:
            best, best_i = score, i
    return best_i


def get_last_judgment() -> dict[str, Any] | None:
    return _last_result


def load_manifest(folder: str) -> dict[str, Any] | None:
    if not folder:
        return None
    path = Path(folder)
    if path.is_file() and path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    if not path.is_dir():
        return None
    for name in ("manifest.json", "manifest_1.json", "manifest_2.json"):
        candidate = path / name
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return None


def image_path_from_cam(cam: dict[str, Any] | None, prefer_ai: bool = True) -> str:
    """UI는 보정본을 보여 준다. 비지도 채점은 학습과 같은 원본을 쓴다."""
    if not cam:
        return ""
    keys = ("ai_file", "file") if prefer_ai else ("file", "ai_file")
    for key in keys:
        path = cam.get(key)
        if path and Path(path).is_file():
            return str(path)
    return ""


def iter_images(manifest: dict[str, Any] | None, inspect_label: str, prefer_ai: bool = True):
    """manifest에서 (cam_id, inspect, file_path) 순회."""
    if not manifest:
        return
    for cam in manifest.get("cameras") or []:
        path = image_path_from_cam(cam, prefer_ai=prefer_ai)
        if not path:
            continue
        yield int(cam["id"]), inspect_label, path


def _score_min() -> float:
    raw = os.environ.get("JUDGE_SCORE_MIN", "0.60")
    try:
        return float(raw)
    except ValueError:
        return 0.60


def _backend_name() -> str:
    env = os.environ.get("JUDGE_BACKEND", "").strip().lower()
    if env in ("mock", "stub"):
        return env
    try:
        from ui_store import SETTINGS_PATH, load_settings

        if SETTINGS_PATH.is_file():
            name = (load_settings().get("judge_backend") or "").strip().lower()
            if name:
                return name
    except Exception:
        pass
    if env:
        return env
    return "unsup"


def _always_ok_face(inspect: str, cam_id: int) -> bool:
    """면 4 (1차 카메라 4)는 결함 판정에서 뺀다."""
    return str(inspect).startswith("1") and int(cam_id) == 4


def _verdict_from_defects(defects: list[Defect]) -> str:
    if not defects:
        return "OK"
    threshold = _score_min()
    for item in defects:
        if item.score >= threshold:
            return "NG"
    return "OK"


def _infer_stub(manifest_1: dict[str, Any], manifest_2: dict[str, Any]) -> JudgmentResult:
    return JudgmentResult(
        verdict="OK",
        defects=[],
        backend="stub",
        message="모델 미연결 — stub은 항상 OK",
    )


def _infer_mock(manifest_1: dict[str, Any], manifest_2: dict[str, Any]) -> JudgmentResult:
    if os.environ.get("JUDGE_MOCK_NG", "").strip() not in ("1", "true", "yes"):
        return JudgmentResult(
            verdict="OK",
            defects=[],
            backend="mock",
            message="mock OK (JUDGE_MOCK_NG=1 이면 테스트 NG)",
        )

    sample_path = ""
    cam_id = 1
    inspect = "1차"
    for cid, label, path in iter_images(manifest_1, "1차"):
        sample_path = path
        cam_id, inspect = cid, label
        break
    if not sample_path:
        for cid, label, path in iter_images(manifest_2, "2차"):
            sample_path = path
            cam_id, inspect = cid, label
            break

    defects = [
        Defect(
            cam_id=cam_id,
            inspect=inspect,
            class_name="mock_defect",
            score=0.91,
            bbox=[420, 180, 580, 340],
        )
    ]
    return JudgmentResult(
        verdict="NG",
        defects=defects,
        backend="mock",
        message=f"mock NG — UI·파이프라인 테스트용 ({sample_path or '이미지 없음'})",
    )


def _live_modes(backend: str) -> tuple[bool, bool]:
    """(비지도, YOLO) 실행 여부. unsup/model 은 둘 다 돌린다."""
    name = (backend or "unsup").strip().lower()
    if name == "yolo":
        return False, True
    if name in ("unsup", "model", "both"):
        return True, True
    return False, False


def _score_row(
    cam_id: int,
    inspect: str,
    class_name: str,
    path: str,
    score: float,
    threshold: float,
    ng: bool,
    bbox: list[int],
    face: str = "",
    source: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = {
        "cam_id": cam_id,
        "inspect": inspect,
        "class_name": class_name,
        "face": face,
        "path": path,
        "score": score,
        "threshold": threshold,
        "ng": ng,
        "bbox": list(bbox),
        "source": source,
    }
    if extra:
        row.update(extra)
    return row


def _infer_unsup(manifest_1: dict[str, Any], manifest_2: dict[str, Any]):
    from unsup_infer import face_key_for, score_image

    defects: list[Defect] = []
    scores: list[dict[str, Any]] = []
    missing: list[str] = []
    errors: list[str] = []
    tried = 0
    for inspect, manifest in (("1차", manifest_1), ("2차", manifest_2)):
        for cam_id, label, path in iter_images(manifest, inspect, prefer_ai=False):
            key = face_key_for(label, cam_id)
            if not key:
                continue
            tried += 1
            try:
                hit = score_image(path, key)
            except FileNotFoundError:
                missing.append(key)
                continue
            except Exception as exc:
                errors.append(f"{key}: {exc}")
                continue
            scores.append(
                _score_row(
                    cam_id,
                    label,
                    "unknown",
                    path,
                    float(hit["score"]),
                    float(hit["threshold"]),
                    bool(hit["ng"]),
                    list(hit["bbox"]),
                    face=key,
                    source="unsup",
                    extra={"ratio": float(hit.get("ratio") or 0.0), "crop_bbox": list(hit.get("crop_bbox") or [])},
                )
            )
            if hit["ng"] and not _always_ok_face(label, cam_id):
                defects.append(
                    Defect(
                        cam_id=cam_id,
                        inspect=label,
                        class_name="unknown",
                        score=float(hit["score"]),
                        bbox=list(hit["bbox"]),
                        threshold=float(hit["threshold"]),
                        crop_bbox=list(hit.get("crop_bbox") or []),
                    )
                )
    return defects, scores, missing, errors, tried


def _infer_yolo(manifest_1: dict[str, Any], manifest_2: dict[str, Any], fill_ok: bool):
    from unsup_infer import face_key_for
    from yolo_infer import detect_image, weights_path

    if not weights_path().is_file():
        return [], [], True, [], 0

    conf = _score_min()
    defects: list[Defect] = []
    scores: list[dict[str, Any]] = []
    errors: list[str] = []
    tried = 0
    for inspect, manifest in (("1차", manifest_1), ("2차", manifest_2)):
        for cam_id, label, path in iter_images(manifest, inspect, prefer_ai=False):
            key = face_key_for(label, cam_id)
            if not key:
                continue
            tried += 1
            try:
                hits = detect_image(path, cam_id, conf=conf, inspect=label)
            except Exception as exc:
                errors.append(f"{key}: {exc}")
                continue
            if fill_ok and not hits:
                scores.append(
                    _score_row(
                        cam_id,
                        label,
                        "unknown",
                        path,
                        0.0,
                        conf,
                        False,
                        [0, 0, 0, 0],
                        face=key,
                        source="yolo",
                    )
                )
            if _always_ok_face(label, cam_id):
                continue
            for hit in hits:
                class_name = str(hit.get("class_name") or "unknown")
                crop_box = list(hit.get("crop_bbox") or [])
                scores.append(
                    _score_row(
                        cam_id,
                        label,
                        class_name,
                        path,
                        float(hit["score"]),
                        conf,
                        True,
                        list(hit["bbox"]),
                        face=key,
                        source="yolo",
                        extra={"crop_bbox": crop_box},
                    )
                )
                defects.append(
                    Defect(
                        cam_id=cam_id,
                        inspect=label,
                        class_name=class_name,
                        score=float(hit["score"]),
                        bbox=list(hit["bbox"]),
                        threshold=conf,
                        crop_bbox=crop_box,
                    )
                )
    return defects, scores, False, errors, tried


def _infer_model(manifest_1: dict[str, Any], manifest_2: dict[str, Any]) -> JudgmentResult:
    """면별 PatchCore + YOLO. 한쪽이라도 NG 이면 불량."""
    backend = _backend_name()
    run_unsup, run_yolo = _live_modes(backend)
    defects: list[Defect] = []
    scores: list[dict[str, Any]] = []
    parts: list[str] = []
    missing: list[str] = []
    errors: list[str] = []
    tried = 0

    if run_unsup:
        u_defects, u_scores, missing, u_errors, u_tried = _infer_unsup(manifest_1, manifest_2)
        defects.extend(u_defects)
        scores.extend(u_scores)
        errors.extend(u_errors)
        tried += u_tried
        unsup_faces = len(u_scores)
        parts.append(f"비지도 PatchCore · {unsup_faces}면")
        if u_defects:
            parts.append(f"이상 {len(u_defects)}면")

    if run_yolo:
        y_defects, y_scores, y_missing, y_errors, y_tried = _infer_yolo(
            manifest_1, manifest_2, fill_ok=not run_unsup
        )
        defects.extend(y_defects)
        scores.extend(y_scores)
        errors.extend(y_errors)
        tried += y_tried
        if y_missing:
            parts.append("YOLO 가중치 없음")
        else:
            parts.append(f"YOLO {len(y_defects)}건")

    if run_unsup and run_yolo:
        result_backend = "unsup+yolo"
    elif run_yolo:
        result_backend = "yolo"
    else:
        result_backend = "unsup"

    if tried == 0:
        return JudgmentResult(
            verdict="OK",
            defects=[],
            scores=scores,
            backend=result_backend,
            message="채점할 면 이미지가 없음",
        )
    unsup_ok = any(row.get("source") == "unsup" for row in scores)
    if run_unsup and not unsup_ok and (missing or errors) and not scores:
        fail = ["비지도 채점 실패"]
        if missing:
            fail.append("없는 모델: " + ", ".join(sorted(set(missing))))
        if errors:
            fail.append("; ".join(errors[:3]))
        return JudgmentResult(
            verdict="NG",
            defects=[],
            scores=scores,
            backend=result_backend,
            message=" · ".join(fail),
        )
    if missing:
        parts.append("없는 비지도 모델: " + ", ".join(sorted(set(missing))))
    if errors:
        parts.append("오류 " + str(len(errors)))
    return JudgmentResult(
        verdict="NG" if defects else "OK",
        defects=defects,
        scores=scores,
        backend=result_backend,
        message=" · ".join(parts),
    )


_BACKENDS = {
    "stub": _infer_stub,
    "mock": _infer_mock,
    "model": _infer_model,
    "unsup": _infer_model,
    "yolo": _infer_model,
    "both": _infer_model,
}


def infer(manifest_1: dict[str, Any] | None, manifest_2: dict[str, Any] | None) -> JudgmentResult:
    """1·2차 manifest dict로 판정."""
    name = manifest_1.get("folder", "") if manifest_1 else ""
    second = manifest_2.get("folder", "") if manifest_2 else ""

    if not manifest_1 or not manifest_2:
        result = JudgmentResult(
            verdict="OK",
            manifest_1=name,
            manifest_2=second,
            backend=_backend_name(),
            message="경고 — 1·2차 촬영이 모두 필요합니다 (현재 OK 처리)",
            judged_at=datetime.now().isoformat(timespec="seconds"),
        )
        _store(result)
        return result

    backend = _backend_name()
    runner = _BACKENDS.get(backend)
    if runner is None:
        result = JudgmentResult(
            verdict="OK",
            manifest_1=name,
            manifest_2=second,
            backend=backend,
            message=f"알 수 없는 JUDGE_BACKEND={backend!r} — stub 처리",
            judged_at=datetime.now().isoformat(timespec="seconds"),
        )
        _store(result)
        return result

    try:
        from sample_roi import archive_operation_crops

        archive_operation_crops(manifest_1, manifest_2)
    except Exception as exc:
        print(f"[crop_cum] 저장 실패: {exc}", flush=True)

    core = runner(manifest_1, manifest_2)
    core.manifest_1 = name
    core.manifest_2 = second
    core.judged_at = datetime.now().isoformat(timespec="seconds")
    if core.verdict not in ("OK", "NG"):
        core.verdict = _verdict_from_defects(core.defects)
    _store(core)
    return core


def infer_from_folders(folder_1: str, folder_2: str) -> JudgmentResult:
    return infer(load_manifest(folder_1), load_manifest(folder_2))


def _store(result: JudgmentResult):
    global _last_result
    payload = result.to_dict()
    _last_result = payload
    RESULT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_standalone_ai_test():
    """12번 AI 단독 — 저장된 manifest 경로가 없으면 안내만."""
    from inspection import get_capture_manifests

    folders = get_capture_manifests()
    result = infer_from_folders(folders.get("1차", ""), folders.get("2차", ""))
    print(f"[12 AI] backend={result.backend} verdict={result.verdict}")
    print(f"[12 AI] defects={len(result.defects)} message={result.message}")
    for item in result.defects:
        print(
            f"  cam{item.cam_id} {item.inspect} {item.class_name} "
            f"{item.score:.2f} bbox={item.bbox}"
        )
    print(f"[12 AI] 저장 → {RESULT_PATH}")
    return result
