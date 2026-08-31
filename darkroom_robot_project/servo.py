"""검사대 샘플 회전 — 서보 전용 아두이노 (CH340).

조명은 FTDI 보드로 따로 간다. 2차 검사는 180° → (조명·촬영) → 원위치 18°.
"""
import time

from arduino_link import send_servo

ANGLE_180 = "180"
ANGLE_90 = "90"
ANGLE_HOME = "18"
REPLY_WAIT = 3.0
# 도는 시간은 보드가 OK 내기 전 1초(SERVO_WAIT_MS)로 끝낸다. 그 뒤 추가는 없다.
SETTLE = 0


def _move(angle, label):
    print(f"[서보] {label}")
    reply = send_servo(angle, timeout=REPLY_WAIT, must_reply=True)
    if reply is None:
        raise ConnectionError(f"서보 보드 미연결 — {angle}")
    if SETTLE > 0:
        time.sleep(SETTLE)
    return reply


def rotate_180():
    """2차 검사용 180°. 보드 OK를 받은 뒤에만 다음 단계."""
    return _move(ANGLE_180, "180°")


def rotate_90():
    """테스트용 90°."""
    return _move(ANGLE_90, "90°")


def home():
    """촬영이 끝나면 18°로 되돌린다."""
    return _move(ANGLE_HOME, "원위치 18°")


def door_open():
    """자동문을 연다."""
    return _move("open", "문 열기")


def door_close():
    """자동문을 닫는다."""
    return _move("close", "문 닫기")
