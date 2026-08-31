"""준비된 단계만 순서대로 실행한다. 한 단계가 OK여야 다음을 시작한다.

2차 검사: 서보 180° OK → 조명 ON·촬영·OFF OK → 서보 18°.
아직 안 하는 것: 샘플 집기, 판정, 분류, 완료 보고(OK/NG UI), 카메라 캘리브(11번), AI 모델(12번).
"""
import time

from inspection import inspection_first, inspection_second
from servo import rotate_180 as servo_rotate_180
from skills import task_bringout, task_flip, task_insert

STAGE_GAP = 1.0

def camera_calib():
    """11번 자리. 대비·초점 등 카메라 세부 조정. 아직 파이프라인에서는 안 한다."""
    print("[캘리브] 자리만 있음 — 대비·초점은 calib_ui에서. 여기서는 건너뜀")


def ai_infer():
    """12번 자리. 어떤 AI 모델을 쓸지 아직 정하지 않았다."""
    print("[AI] 자리만 있음 — 모델 미정. 여기서는 건너뜀")


STEPS = {
    "INSERT": ("투입", task_insert),
    "INSPECT_1": ("1차 검사", inspection_first),
    "FLIP": ("뒤집기", task_flip),
    "SERVO": ("서보 180도", servo_rotate_180),
    "INSPECT_2": ("2차 검사", inspection_second),
    "BRINGOUT": ("회수", task_bringout),
    "CALIB": ("카메라 캘리브", camera_calib),
    "AI": ("AI 추론", ai_infer),
}

READY_SEQUENCE = (
    "INSERT",
    "INSPECT_1",
    "GAP",
    "FLIP",
    "INSPECT_2",
    "GAP",
    "BRINGOUT",
)


def run_step(command):
    """단계 하나 실행. (DONE, 메시지) 또는 GAP은 (DONE, 대기)."""
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
    func()
    print(f"[파이프라인] {title} 완료 → OK")
    return "DONE", f"{title} 완료"


def run_ready_sequence():
    """투입 → 1차검사 → 뒤집기 → 2차검사 → 회수."""
    print("\n########## 준비된 파이프라인 시작 ##########")
    for command in READY_SEQUENCE:
        status, message = run_step(command)
        if status != "DONE":
            raise RuntimeError(message)
    print("########## 준비된 파이프라인 완료 ##########\n")
