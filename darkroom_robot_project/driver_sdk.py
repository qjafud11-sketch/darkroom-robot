"""Feetech STS3215 시리얼 드라이버 (SO-ARM101 6축).

J1 베이스 · J2 어깨 · J3 팔꿈치 · J4 손목(상하) · J5 손목(롤) · J6 그리퍼
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import serial

# Feetech 프로토콜 명령
INST_PING = 0x01
INST_READ = 0x02
INST_WRITE = 0x03

# 레지스터 주소
ADDR_TORQUE_ENABLE = 40
ADDR_GOAL_POSITION = 42
ADDR_GOAL_SPEED = 46
ADDR_PRESENT_POSITION = 56
ADDR_PRESENT_LOAD = 60
ADDR_PRESENT_VOLTAGE = 62
ADDR_PRESENT_TEMPERATURE = 63

# 관절 이름 (J1~J6) — 로그·상태 출력용
JOINT_NAMES = ["베이스", "어깨", "팔꿈치", "손목상하", "손목롤", "그리퍼"]
JOINT_IDS = [1, 2, 3, 4, 5, 6]

POS_MIN = 0
POS_MAX = 4095
# 실제 가동범위 확인용 진단 모드: joint_limits.json 클램프를 적용하지 않는다.
# 서보 내부의 과전류·과열 보호는 이 설정과 무관하게 유지된다.
ENFORCE_JOINT_LIMITS = False


def _load_joint_limits() -> dict[int, tuple[int, int]]:
    """캘리브레이션된 관절별 안전 범위를 읽는다."""
    path = Path(__file__).with_name("joint_limits.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        limits = {}
        for name, values in data["joints"].items():
            joint_id = int(name.removeprefix("J"))
            minimum, maximum = int(values["min"]), int(values["max"])
            if joint_id not in JOINT_IDS or not POS_MIN <= minimum < maximum <= POS_MAX:
                raise ValueError(f"잘못된 {name} 범위: {minimum}~{maximum}")
            limits[joint_id] = (minimum, maximum)
        if set(limits) != set(JOINT_IDS):
            raise ValueError("J1~J6 범위가 모두 필요합니다")
        return limits
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"[드라이버] joint_limits.json 읽기 실패 — 기본 범위 사용: {exc}")
        return {sid: (POS_MIN, POS_MAX) for sid in JOINT_IDS}


JOINT_LIMITS = _load_joint_limits()
J1_POS_MIN, J1_POS_MAX = JOINT_LIMITS.get(1, (877, 3286))


@dataclass
class JointStatus:
    id: int
    name: str
    online: bool = False
    position: Optional[int] = None
    temperature: Optional[int] = None
    voltage: Optional[float] = None
    load: Optional[int] = None
    torque_enabled: Optional[bool] = None


class STS3215Driver:
    def __init__(self, port: str = "/dev/ttyACM0", baudrate: int = 1_000_000, timeout: float = 0.02):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial: Optional[serial.Serial] = None
        self._lock = threading.Lock()

    def connect(self) -> bool:
        try:
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
            )
            self.serial.reset_input_buffer()
            self.serial.reset_output_buffer()
            time.sleep(0.1)
            return True
        except Exception as exc:
            print(f"[드라이버] 연결 오류: {exc}")
            self.serial = None
            return False

    def disconnect(self) -> None:
        if self.serial and self.serial.is_open:
            self.serial.close()
        self.serial = None

    def is_connected(self) -> bool:
        return self.serial is not None and self.serial.is_open

    @staticmethod
    def _checksum(packet: list[int]) -> int:
        return (~sum(packet[2:])) & 0xFF

    def _build_packet(self, servo_id: int, instruction: int, params: Optional[list[int]] = None) -> bytes:
        params = params or []
        length = len(params) + 2
        pkt = [0xFF, 0xFF, servo_id, length, instruction, *params]
        pkt.append(self._checksum(pkt))
        return bytes(pkt)

    def _transact(
        self,
        servo_id: int,
        instruction: int,
        params: Optional[list[int]] = None,
        response_len: int = 0,
    ) -> Optional[dict]:
        with self._lock:
            if not self.is_connected():
                return None
            packet = self._build_packet(servo_id, instruction, params)
            try:
                self.serial.reset_input_buffer()
                self.serial.write(packet)
                self.serial.flush()
                if response_len > 0:
                    expected = 6 + response_len
                    response = self.serial.read(expected)
                    if len(response) >= 6 and response[0] == 0xFF and response[1] == 0xFF:
                        error = response[4]
                        data = list(response[5:-1]) if len(response) > 6 else []
                        return {"id": response[2], "error": error, "data": data}
                return None
            except Exception as exc:
                print(f"[드라이버] 통신 오류: {exc}")
                return None

    def ping(self, servo_id: int) -> bool:
        with self._lock:
            if not self.is_connected():
                return False
            packet = self._build_packet(servo_id, INST_PING)
            try:
                self.serial.reset_input_buffer()
                self.serial.write(packet)
                self.serial.flush()
                response = self.serial.read(6)
                return len(response) >= 6 and response[0] == 0xFF and response[1] == 0xFF
            except Exception:
                return False

    def _read(self, servo_id: int, addr: int, length: int) -> Optional[list[int]]:
        result = self._transact(servo_id, INST_READ, [addr, length], length)
        if result and result["error"] == 0 and len(result["data"]) >= length:
            return result["data"]
        return None

    def _write(self, servo_id: int, addr: int, data: list[int]) -> bool:
        params = [addr, *data]
        with self._lock:
            if not self.is_connected():
                return False
            packet = self._build_packet(servo_id, INST_WRITE, params)
            try:
                self.serial.reset_input_buffer()
                self.serial.write(packet)
                self.serial.flush()
                response = self.serial.read(6)
                if len(response) >= 6:
                    return response[4] == 0
                return True
            except Exception as exc:
                print(f"[드라이버] 쓰기 오류: {exc}")
                return False

    def _read_u16(self, servo_id: int, addr: int) -> Optional[int]:
        data = self._read(servo_id, addr, 2)
        if data and len(data) >= 2:
            return data[0] | (data[1] << 8)
        return None

    def _write_u16(self, servo_id: int, addr: int, value: int) -> bool:
        value = max(0, min(65535, int(value)))
        return self._write(servo_id, addr, [value & 0xFF, (value >> 8) & 0xFF])

    def _write_u8(self, servo_id: int, addr: int, value: int) -> bool:
        value = max(0, min(255, int(value)))
        return self._write(servo_id, addr, [value])

    def get_position(self, servo_id: int) -> Optional[int]:
        return self._read_u16(servo_id, ADDR_PRESENT_POSITION)

    def set_position(self, servo_id: int, position: int) -> bool:
        return self._write_u16(servo_id, ADDR_GOAL_POSITION, self.clamp_position(servo_id, position))

    @staticmethod
    def clamp_position(servo_id: int, position: int) -> int:
        position = max(POS_MIN, min(POS_MAX, int(position)))
        if not ENFORCE_JOINT_LIMITS:
            return position
        minimum, maximum = JOINT_LIMITS.get(servo_id, (POS_MIN, POS_MAX))
        return max(minimum, min(maximum, position))

    def set_speed(self, servo_id: int, speed: int) -> bool:
        speed = max(0, min(4095, int(speed)))
        return self._write_u16(servo_id, ADDR_GOAL_SPEED, speed)

    def set_torque(self, servo_id: int, enable: bool) -> bool:
        return self._write_u8(servo_id, ADDR_TORQUE_ENABLE, 1 if enable else 0)

    def get_torque(self, servo_id: int) -> Optional[bool]:
        data = self._read(servo_id, ADDR_TORQUE_ENABLE, 1)
        return data[0] == 1 if data else None

    def get_temperature(self, servo_id: int) -> Optional[int]:
        data = self._read(servo_id, ADDR_PRESENT_TEMPERATURE, 1)
        return data[0] if data else None

    def get_voltage(self, servo_id: int) -> Optional[float]:
        data = self._read(servo_id, ADDR_PRESENT_VOLTAGE, 1)
        return data[0] / 10.0 if data else None

    def get_load(self, servo_id: int) -> Optional[int]:
        val = self._read_u16(servo_id, ADDR_PRESENT_LOAD)
        if val is None:
            return None
        load = val & 0x3FF
        return -load if (val & 0x0400) else load

    def get_all_positions(self) -> dict[int, Optional[int]]:
        return {sid: self.get_position(sid) for sid in JOINT_IDS}

    def set_all_positions(self, positions: dict[int, int]) -> dict[int, int]:
        applied = {}
        for sid, pos in positions.items():
            if pos is not None:
                applied[sid] = self.clamp_position(sid, pos)
                self.set_position(sid, applied[sid])
        return applied

    def set_all_torque(self, enable: bool) -> None:
        for sid in JOINT_IDS:
            self.set_torque(sid, enable)

    def get_all_status(self) -> dict[int, JointStatus]:
        out: dict[int, JointStatus] = {}
        for sid in JOINT_IDS:
            online = self.ping(sid)
            if online:
                out[sid] = JointStatus(
                    id=sid,
                    name=JOINT_NAMES[sid - 1],
                    online=True,
                    position=self.get_position(sid),
                    temperature=self.get_temperature(sid),
                    voltage=self.get_voltage(sid),
                    load=self.get_load(sid),
                    torque_enabled=self.get_torque(sid),
                )
            else:
                out[sid] = JointStatus(id=sid, name=JOINT_NAMES[sid - 1], online=False)
        return out
