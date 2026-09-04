"""검사대 샘플 회전 — 서보 전용 아두이노 (CH340).

조명은 FTDI 보드로 따로 간다. 2차 검사는 180° → (조명·촬영) → 원위치 0°.

원위치가 0°라야 180°를 다 돈다. 예전처럼 18°에서 시작하면 162°밖에 안 돌았다.
펌웨어의 SERVO_HOME 과 같은 값이어야 한다.
"""
import time

from arduino_link import send_servo, servo_has_doors

ANGLE_180 = "180"
ANGLE_90 = "90"
ANGLE_HOME = "0"
REPLY_WAIT = 5.0
# 보드 OK 직후에도 거치대·시료 관성으로 조금 더 돈다. 촬영 전에 여기서 한 번 더 쉰다.
SETTLE = 1.0


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
    """촬영이 끝나면 0°로 되돌린다."""
    return _move(ANGLE_HOME, "원위치 0°")


def door_open():
    """자동문을 연다. 펌웨어에 문 명령이 없으면 건너뛴다."""
    if not servo_has_doors():
        print("[서보] 문 열기 건너뜀 — 보드가 open/close 를 모름")
        return None
    return _move("open", "문 열기")


def door_close():
    """자동문을 닫는다. 펌웨어에 문 명령이 없으면 건너뛴다."""
    if not servo_has_doors():
        print("[서보] 문 닫기 건너뜀 — 보드가 open/close 를 모름")
        return None
    return _move("close", "문 닫기")
