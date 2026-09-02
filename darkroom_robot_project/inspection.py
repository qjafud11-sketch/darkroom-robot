"""암실 검사 — 투입/뒤집기 끝난 뒤 조명·카메라.

조명은 FTDI 아두이노, 서보는 CH340 아두이노. PC가 둘 다 OK를 받은 뒤에만 다음으로 간다.
조명이 켜진 뒤에 캡처하고, 저장이 끝나야 조명을 끈다.
8번 판정은 judgment.infer_from_folders — AI는 judgment._infer_model() 에 연결.
"""
import time

from arduino_link import resolve_port, resolve_servo_port, send_ok
from camera import capture
from judgment import infer_from_folders
from light_tone import light_command
from servo import home as servo_home
from servo import rotate_180 as servo_rotate_180

# 흰색(B:80) 대신 캘리브한 톤을 쓴다. 흰색은 카메라가 파란 채널부터 포화시켜
# 노출을 못 올린다. 톤을 맞추면 세 채널이 같이 차서 같은 전류로 더 밝게 찍힌다.
LED_ON = light_command()
LED_OFF = "OFF"
LIGHT_ON = 2.0
SERVO_MOVE_GAP = 0
LIGHT_DONE_GAP = 0

_capture_manifests: dict[str, str] = {}


def _need(reply, board, command):
    if reply is None:
        raise ConnectionError(f"{board} 보드 미연결 — {command}")
    return reply


def control_led(cmd):
    """조명 켜기/끄기. FTDI 보드 OK를 기다린다."""
    if cmd == "LED_ON":
        print(f"[조명] ON  {resolve_port()}")
        _need(send_ok(LED_ON), "조명", LED_ON)
    elif cmd == "LED_OFF":
        print(f"[조명] OFF  {resolve_port()}")
        _need(send_ok(LED_OFF), "조명", LED_OFF)


def get_capture_manifests():
    return dict(_capture_manifests)


def reset_capture_manifests():
    _capture_manifests.clear()


def _inspect(label):
    """조명 ON → 그 직후 촬영 → 켜진 지 2초가 안 됐으면 나머지를 채운 뒤 OFF."""
    control_led("LED_ON")
    started = time.monotonic()
    folder = capture(label)
    _capture_manifests[label] = folder
    remain = LIGHT_ON - (time.monotonic() - started)
    if remain > 0:
        time.sleep(remain)
    control_led("LED_OFF")


def inspection_first():
    """1차 검사: 조명 ON(OK) → 촬영 저장(OK) → 조명 OFF(OK). 서보는 안 건드린다."""
    print(f"\n[검사] 1차 — 투입 완료 후  조명 {resolve_port()}")
    _inspect("1차")


def inspection_second():
    """2차 검사: 서보 180° OK → 조명 ON·촬영·OFF OK → 서보 0°.

    서보 CH340 D8, 조명 FTDI D7. 보드끼리 직접 말하지 않고 PC가 중계한다.
    """
    light_port = resolve_port()
    servo_port = resolve_servo_port()
    print(f"\n[검사] 2차 — 뒤집기 완료 후  서보 {servo_port}  조명 {light_port}")

    print("[통신] PC → 서보  180°")
    servo_rotate_180()
    print("[통신] 서보 → PC  OK 180° → 조명")
    if SERVO_MOVE_GAP > 0:
        time.sleep(SERVO_MOVE_GAP)

    print("[통신] PC → 조명  ON → 촬영 → OFF")
    _inspect("2차")
    print("[통신] 조명 → PC  OK OFF → 원위치")
    if LIGHT_DONE_GAP > 0:
        time.sleep(LIGHT_DONE_GAP)

    print("[통신] PC → 서보  0°")
    servo_home()
    print("[통신] 서보 → PC  OK 0°")


def judge_product():
    """8번 OK/NG — 1·2차 촬영 폴더 → judgment 모듈.

    Returns:
        "OK" 또는 "NG". 상세(defects)는 ~/darkroom_last_judgment.json
    """
    first_dir = _capture_manifests.get("1차", "")
    second_dir = _capture_manifests.get("2차", "")

    print(f"[판정] 1차={first_dir or '(없음)'}")
    print(f"[판정] 2차={second_dir or '(없음)'}")

    result = infer_from_folders(first_dir, second_dir)
    print(f"[판정] backend={result.backend} → {result.verdict}")
    if result.message:
        print(f"[판정] {result.message}")
    for item in result.defects:
        print(
            f"[판정]   NG cam{item.cam_id} {item.inspect} "
            f"{item.class_name} {item.score:.2f} bbox={item.bbox}"
        )
    return result.verdict
