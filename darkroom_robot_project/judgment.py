"""판정 — 1·2차 manifest → verdict + defects.

AI 모델은 나중에 `_infer_model()` 안에만 연결하면 된다.
파이프라인·UI는 이 모듈의 결과 형식(JSON)만 보면 된다.

환경 변수:
  JUDGE_BACKEND   stub(기본) | mock | model
  JUDGE_MODEL_PATH   model 백엔드용 가중치 경로 (미구현)
  JUDGE_SCORE_MIN    NG로 볼 최소 confidence (기본 0.5)
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
    bbox: list[int]  # [x1, y1, x2, y2] — 1280×720 기준

    def to_dict(self):
        return asdict(self)


@dataclass
class JudgmentResult:
    verdict: str  # OK | NG
    defects: list[Defect] = field(default_factory=list)
    manifest_1: str = ""
    manifest_2: str = ""
    backend: str = "stub"
    message: str = ""
    judged_at: str = ""

    def to_dict(self):
        data = asdict(self)
        data["defects"] = [d.to_dict() if isinstance(d, Defect) else d for d in self.defects]
        return data


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


def image_path_from_cam(cam: dict[str, Any] | None) -> str:
    """판정·UI는 보정본이 있으면 그걸 쓴다. 원본만 있으면 원본."""
    if not cam:
        return ""
    for key in ("ai_file", "file"):
        path = cam.get(key)
        if path and Path(path).is_file():
            return str(path)
    return ""


def iter_images(manifest: dict[str, Any] | None, inspect_label: str):
    """manifest에서 (cam_id, inspect, file_path) 순회."""
    if not manifest:
        return
    for cam in manifest.get("cameras") or []:
        path = image_path_from_cam(cam)
        if not path:
            continue
        yield int(cam["id"]), inspect_label, path


def _score_min() -> float:
    raw = os.environ.get("JUDGE_SCORE_MIN", "0.5")
    try:
        return float(raw)
    except ValueError:
        return 0.5


def _backend_name() -> str:
    return os.environ.get("JUDGE_BACKEND", "stub").strip().lower() or "stub"


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


def _infer_model(manifest_1: dict[str, Any], manifest_2: dict[str, Any]) -> JudgmentResult:
    """YOLO / OpenVINO 등 — 모델 파일 준비 후 여기에 추론 코드를 넣는다."""
    model_path = os.environ.get("JUDGE_MODEL_PATH", "").strip()
    if not model_path:
        raise FileNotFoundError(
            "JUDGE_MODEL_PATH 가 없습니다. stub/mock 으로 테스트하거나 모델 경로를 설정하세요."
        )
    if not Path(model_path).is_file():
        raise FileNotFoundError(f"모델 파일 없음: {model_path}")

    # TODO: for cam_id, inspect, path in chain(iter_images(m1,'1차'), iter_images(m2,'2차')):
    #           detections = run_model(model_path, path)
    #           defects.extend(...)
    raise NotImplementedError(
        f"model 백엔드 골격만 준비됨 — {model_path} 에 대한 추론 코드를 judgment._infer_model()에 추가"
    )


_BACKENDS = {
    "stub": _infer_stub,
    "mock": _infer_mock,
    "model": _infer_model,
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
