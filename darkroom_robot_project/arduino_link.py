"""PC ↔ 아두이노 두 대.

조명: FTDI A5069RR4, NeoPixel D7
서보: CH340, SG90 D8 (배너 READY SERVO)
여분 CH340(허브 5.2)과 로봇팔 ACM은 열지 않는다.
"""
from pathlib import Path

import serial
import time

from hw_ports import EXTRA_CH340_BY_PATH, LIGHT_BY_ID, LIGHT_FALLBACK, resolve_servo_candidates

BAUD = 9600
RESET_WAIT = 2.5
REPLY_TIMEOUT = 2.0
MOVE_WAIT = 0.5
# 포트를 열면 보드가 리셋된다. RESET_WAIT 만으로 배너를 못 받는 경우가 있어서
# 배너 글자를 볼 때까지 이만큼 더 기다린다. 첫 연결에서만 든다.
BANNER_WAIT = 3.0

# 조명 밝기. 카메라 캘리브를 이 밝기에서 맞췄으니 검사도 테스트 UI도 같은 값을 써야 한다.
# 다른 밝기로 켜고 보면 캘리브한 노출과 안 맞는다.
# LIGHT_MAX 는 펌웨어의 MAX_BRIGHTNESS 와 같아야 한다.
LIGHT_MAX = 80
LIGHT_BRIGHTNESS = 80

LIGHT_CANDIDATES = (
    LIGHT_BY_ID,
    LIGHT_FALLBACK,
)

CONSOLE_MARKERS = ("login:", "Debian", "raspberrypi", "ttyAMA")


def _resolve(path):
    p = Path(path)
    if not p.exists():
        return None
    return str(p.resolve())


def _is_extra_ch340(port):
    extra = _resolve(EXTRA_CH340_BY_PATH)
    got = _resolve(port)
    return bool(extra and got and extra == got)


def servo_candidate_ports():
    """CH340 중에서 조명 FTDI·여분 CH340을 뺀 것."""
    return resolve_servo_candidates()


class ArduinoBoard:
    def __init__(self, name, candidates, ready_hint=""):
        self.name = name
        self.candidates = candidates
        self.ready_hint = ready_hint
        self.serial = None
        self.opened = False
        self.port = None
        self.banner = ""

    def resolve_port(self):
        if callable(self.candidates):
            return list(self.candidates())
        seen = []
        for candidate in self.candidates:
            resolved = _resolve(candidate)
            if not resolved or resolved in seen:
                continue
            if self.ready_hint == "SERVO" and _is_extra_ch340(resolved):
                continue
            seen.append(resolved)
        return seen

    def _open_one(self, port):
        """포트를 열고 부팅 배너를 다 받아낸다.

        배너를 흘리면 다음에 보내는 첫 명령의 응답 자리에 배너가 들어온다.
        서보에서 그러면 안 돌았는데 OK 로 보인다. 그래서 배너를 볼 때까지 기다린다.
        """
        ser = serial.Serial(port, BAUD, timeout=0.2)
        time.sleep(RESET_WAIT)
        buf = bytearray()
        idle = 0
        deadline = time.monotonic() + BANNER_WAIT
        while True:
            waiting = ser.in_waiting
            if waiting:
                buf.extend(ser.read(waiting))
                idle = 0
                continue
            if self.ready_hint and self.ready_hint in buf.decode("ascii", errors="replace"):
                break
            idle += 1
            if idle >= 6 and time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        leftover = buf.decode("ascii", errors="replace").strip()
        return ser, leftover

    def _skip_reason(self, leftover):
        if leftover and any(mark in leftover for mark in CONSOLE_MARKERS):
            return "여분 CH340 (서보 아님)"
        if self.ready_hint and leftover and self.ready_hint not in leftover:
            return leftover
        return None

    def _get(self):
        if self.serial is not None:
            return self.serial

        ports = self.resolve_port()
        if not ports:
            print(f"[경고] {self.name} 포트 없음")
            return None

        last_error = None
        for port in ports:
            if _is_extra_ch340(port):
                print(f"[{self.name}] {port} 건너뜀 (여분 CH340)")
                continue
            try:
                ser, leftover = self._open_one(port)
            except Exception as exc:
                last_error = exc
                continue
            reason = self._skip_reason(leftover)
            if reason:
                print(f"[{self.name}] {port} 건너뜀 ({reason})")
                ser.close()
                continue
            self.serial = ser
            self.port = port
            self.banner = leftover
            print(f"[{self.name}] 포트 {port}")
            if leftover:
                print(f"[{self.name}] 접속: {leftover}")
            return self.serial

        print(f"[경고] {self.name} 연결 실패: {last_error or '배너 맞는 보드 없음'}")
        self.serial = None
        return None

    def send_ok(self, command, timeout=REPLY_TIMEOUT, must_reply=True):
        ser = self._get()
        if ser is None:
            print(f"[{self.name}] 미연결 — 생략: {command}")
            return None

        line = command.strip()
        # READY 는 부팅 배너지 명령 응답이 아니다. 그걸 OK 로 받으면
        # 부팅 중에 삼켜진 명령을 성공으로 착각한다. 배너를 보면 한 번 다시 보낸다.
        for attempt in range(2):
            ser.reset_input_buffer()
            ser.write((line + "\n").encode("ascii"))
            ser.flush()
            print(f"[{self.name}] → {line}")

            booted = False
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                raw = ser.readline()
                if not raw:
                    continue
                reply = raw.decode("ascii", errors="replace").strip()
                if not reply:
                    continue
                print(f"[{self.name}] ← {reply}")
                if any(mark in reply for mark in CONSOLE_MARKERS):
                    raise RuntimeError(f"{self.name} 포트가 해당 아두이노가 아닙니다: {self.port}")
                if reply.startswith("ERR"):
                    raise RuntimeError(f"{self.name} 거부: {reply} (명령 {line})")
                if reply.startswith("OK"):
                    return reply
                if reply.startswith("READY"):
                    booted = True
                    break

            if not (booted and attempt == 0):
                break
            print(f"[{self.name}] 방금 부팅한 보드였음 — 다시 보냄")

        if not must_reply:
            print(f"[{self.name}] OK 없음 — {MOVE_WAIT}초 대기 후 진행")
            time.sleep(MOVE_WAIT)
            return None

        raise TimeoutError(f"{self.name} OK 없음: {line}")


_light = ArduinoBoard("조명", LIGHT_CANDIDATES, ready_hint="NeoPixel")
_servo = ArduinoBoard("서보", servo_candidate_ports, ready_hint="SERVO")


def resolve_port():
    """조명 보드 경로. light_test_ui용."""
    ports = _light.resolve_port()
    return ports[0] if ports else LIGHT_CANDIDATES[-1]


def resolve_servo_port():
    """서보 보드 경로. 아직 안 열었으면 후보 첫 값."""
    if _servo.port:
        return _servo.port
    ports = _servo.resolve_port()
    return ports[0] if ports else ""


def send_ok(command, timeout=REPLY_TIMEOUT, must_reply=True):
    """조명 보드에 한 줄 보내고 OK를 기다린다."""
    return _light.send_ok(command, timeout=timeout, must_reply=must_reply)


def send_servo(command, timeout=REPLY_TIMEOUT, must_reply=True):
    """서보 보드에 각도 숫자를 보내고 OK를 기다린다."""
    return _servo.send_ok(command, timeout=timeout, must_reply=must_reply)


def servo_banner() -> str:
    """서보 보드 부팅 배너. 필요하면 포트를 연다."""
    _servo._get()
    return _servo.banner or ""


def servo_has_doors() -> bool:
    """플래시된 펌웨어가 open/close 문을 아는지."""
    return "DOOR" in servo_banner()
