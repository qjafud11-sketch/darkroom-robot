"""암실 검사 — 카메라·센서 연동 시 이 파일만 구현하면 됩니다."""
import serial
import time

# 아두이노 시리얼 통신 설정 (라즈베리파이에 연결된 포트, 리눅스 기본)
try:
    arduino = serial.Serial('/dev/ttyUSB0', 9600, timeout=1)
    time.sleep(2) # 아두이노 재부팅 대기
except Exception as e:
    print(f"[경고] 아두이노 연결 실패 (조명 제어 무시됨): {e}")
    arduino = None

def control_led(cmd):
    """아두이노로 시리얼 조명 명령 전달"""
    if arduino:
        if cmd == "LED_ON":
            arduino.write(b"B:30\n")  # 비전 검사용 밝기(30)로 켬
            print("💡 [조명] ON")
        elif cmd == "LED_OFF":
            arduino.write(b"OFF\n")   # 조명 끄기
            print("💡 [조명] OFF")

def inspection_first():
    """1차 검사 (투입 직후). 촬영·측정 코드를 넣을 자리."""
    print("\n[검사] 1차 — 투입 완료 후")
    control_led("LED_ON")
    time.sleep(1.0) # 1초간 비전 검사 수행 대기
    control_led("LED_OFF")


def inspection_second():
    """2차 검사 (뒤집기 직후). 뒤집힌 샘플 확인."""
    print("\n[검사] 2차 — 뒤집기 완료 후")
    control_led("LED_ON")
    time.sleep(1.0) # 1초간 비전 검사 수행 대기
    control_led("LED_OFF")


def judge_product():
    """OK/NG 판정. 아직 미구현 — 항상 OK 반환."""
    return "OK"
