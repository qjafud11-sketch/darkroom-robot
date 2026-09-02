"""목표 순서(0~12) 파이프라인 — 사진 표 기준.

자동 시퀀스 FULL_SEQUENCE (0~10, UI 실행):
  0 집기 → 1 투입 → 2 1차검사 → GAP → 4 뒤집기 → 5 2차검사 → GAP
  → 7 회수+8 판정(동시) → 9 분류 → 10 완료보고

CLI `main.py run` 은 READY_SEQUENCE (투입~회수, 집기·분류 없음).

11 카메라 캘리브 · 12 AI — 단독 명령만 (자동 시퀀스 제외).
"""
import concurrent.futures
import time

from inspection import (
    inspection_first,
    inspection_second,
    judge_product,
    reset_capture_manifests,
)
from judgment import get_last_judgment, run_standalone_ai_test
from servo import rotate_180 as servo_rotate_180
from skills import task_bringout, task_flip, task_insert, task_pick, task_sort

STAGE_GAP = 1.0

# 0 집기만 아직 포즈가 없다. True면 NotImplementedError 를 건너뛴다.
SKIP_UNIMPLEMENTED = True

# True면 로봇팔(집기·투입·뒤집기·회수·분류)을 움직이지 않는다.
# 연속 검사에서 조명·카메라·서보·판정만 돌릴 때 켠다.
SKIP_ROBOT_ARM = True
ARM_COMMANDS = ("PICK", "INSERT", "FLIP", "SORT")

_state = {"verdict": "OK", "judgment": None}


def reset_state():
    _state["verdict"] = "OK"
    _state["judgment"] = None


def get_verdict():
    return _state["verdict"]


def get_judgment():
    """최근 판정 전체 — verdict, defects[], manifest 경로."""
    return _state.get("judgment") or get_last_judgment()


def get_ui_snapshot():
    """GUI·robot_client 응답용 — 판정·촬영 경로."""
    from inspection import get_capture_manifests

    return {
        "verdict": _state["verdict"],
        "judgment": get_judgment(),
        "captures": get_capture_manifests(),
    }


def camera_calib():
    """11 — calib_ui.py 수동. 자동 시퀀스 제외."""
    print("[11 캘리브] calib_ui.py — 파이프라인에서는 건너뜀")


def ai_infer():
    """12 — 판정 백엔드 단독 테스트 (JUDGE_BACKEND=model 등)."""
    result = run_standalone_ai_test()
    _state["verdict"] = result.verdict
    _state["judgment"] = result.to_dict()
    return result.verdict


def run_judge():
    """8 — 1·2차 촬영본 기준 OK/NG."""
    verdict = judge_product()
    _state["verdict"] = verdict
    _state["judgment"] = get_last_judgment()
    print(f"[8 판정] {verdict}")
    return verdict


def run_bringout_with_judge():
    """7+8 — 회수(로봇)와 판정(1·2차 데이터)을 동시에."""
    if SKIP_ROBOT_ARM:
        print("[7+8] 회수 건너뜀 — 로봇팔 정지, 판정만")
        run_judge()
        print(f"[7+8] 판정 완료 — {_state['verdict']}")
        return

    print("[7+8] 회수 + 판정 동시 시작")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut_bringout = pool.submit(task_bringout)
        fut_judge = pool.submit(run_judge)
        concurrent.futures.wait([fut_bringout, fut_judge])
        fut_bringout.result()
        fut_judge.result()

    print(f"[7+8] 회수·판정 완료 — {_state['verdict']}")


def run_sort():
    """9 — OK/NG에 따라 분류 포즈."""
    verdict = _state["verdict"]
    print(f"[9 분류] {verdict}")
    task_sort(verdict)


def run_report():
    """10 — UI·로그에 최종 결과."""
    print(f"[10 완료 보고] {_state['verdict']}")


STEPS = {
    "PICK": ("0 샘플 집기", task_pick),
    "INSERT": ("1 투입", task_insert),
    "INSPECT_1": ("2 1차 검사", inspection_first),
    "FLIP": ("4 뒤집기", task_flip),
    "SERVO": ("서보 180° (단독)", servo_rotate_180),
    "INSPECT_2": ("5 2차 검사", inspection_second),
    "BRINGOUT": ("7 회수 + 8 판정", run_bringout_with_judge),
    "JUDGE": ("8 판정 (단독)", run_judge),
    "SORT": ("9 분류", run_sort),
    "REPORT": ("10 완료 보고", run_report),
    "CALIB": ("11 카메라 캘리브", camera_calib),
    "AI": ("12 AI 추론", ai_infer),
}

FULL_SEQUENCE = (
    "PICK",
    "INSERT",
    "INSPECT_1",
    "GAP",
    "FLIP",
    "INSPECT_2",
    "GAP",
    "BRINGOUT",
    "SORT",
    "REPORT",
)

READY_SEQUENCE = (
    "INSERT",
    "INSPECT_1",
    "GAP",
    "FLIP",
    "INSPECT_2",
    "GAP",
    "BRINGOUT",
)

PIPELINE_COMMANDS = (
    "PING",
    *FULL_SEQUENCE,
    "SERVO",
    "JUDGE",
    "CALIB",
    "AI",
)


def run_step(command):
    """단계 하나 실행. (상태, 메시지) 반환."""
    if command == "PING":
        print("[파이프라인] 통신 확인")
        return "DONE", "통신 확인"

    if command == "GAP":
        if STAGE_GAP > 0:
            print(f"[파이프라인] 단계 사이 {STAGE_GAP}초")
            time.sleep(STAGE_GAP)
        return "DONE", "단계 사이 완료"

    if command not in STEPS:
        return "ERROR", f"{command} 명령을 찾을 수 없음"

    title, func = STEPS[command]
    print(f"[파이프라인] {title} 시작")

    if SKIP_ROBOT_ARM and command in ARM_COMMANDS:
        print(f"[파이프라인] {title} 건너뜀 — 로봇팔 정지")
        return "DONE", f"{title} 건너뜀 (로봇팔 정지)"

    try:
        func()
    except NotImplementedError as exc:
        if SKIP_UNIMPLEMENTED and command in ("PICK", "SORT"):
            print(f"[파이프라인] {title} 미구현 — 건너뜀 ({exc})")
            return "DONE", f"{title} 건너뜀 (미구현)"
        raise

    print(f"[파이프라인] {title} 완료 → OK")

    if command == "REPORT":
        return "DONE", f"완료 보고: {_state['verdict']}"
    if command == "BRINGOUT":
        return "DONE", f"회수·판정: {_state['verdict']}"
    if command == "JUDGE":
        return "DONE", f"판정: {_state['verdict']}"
    return "DONE", f"{title} 완료"


def run_full_sequence():
    """0~10번 전체 (사진 표 순서)."""
    reset_state()
    reset_capture_manifests()
    print("\n########## 목표 파이프라인 시작 (0~10) ##########")
    for command in FULL_SEQUENCE:
        status, message = run_step(command)
        if status != "DONE":
            raise RuntimeError(message)
    print(f"########## 목표 파이프라인 완료 — {_state['verdict']} ##########\n")
    return _state["verdict"]


def run_ready_sequence():
    """투입 → 1차검사 → 뒤집기 → 2차검사 → 회수(+판정). CLI `main.py run`."""
    reset_state()
    reset_capture_manifests()
    print("\n########## 준비된 파이프라인 시작 ##########")
    for command in READY_SEQUENCE:
        status, message = run_step(command)
        if status != "DONE":
            raise RuntimeError(message)
    print(f"########## 준비된 파이프라인 완료 — {_state['verdict']} ##########\n")
    return _state["verdict"]
