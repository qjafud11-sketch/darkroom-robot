import socket
import json
import time

# 깃허브 로봇팔 코드에서 실제 동작 함수들을 임포트합니다.
from skills import task_insert, task_flip, task_bringout
from inspection import inspection_first, inspection_second

HOST = '127.0.0.1' # 로컬 테스트 시 127.0.0.1, 실제 NUC(서버)가 다른 PC라면 해당 IP로 변경
PORT = 8585

def handle_command(command):
    """명령 하나를 실행하고 (상태, 메시지)를 돌려준다.

    로봇 연결 끊김이나 스텝 실패로 예외가 나도 프로세스를 죽이지 않고
    ERROR로 회신한다. 클라이언트를 손으로 다시 켜지 않아도 다음 명령을 받는다.
    """
    try:
        if command == "INSERT":
            print(" 🤖 [동작] 1차 투입 및 검사 시작...")
            task_insert()
            inspection_first() # 조명 1초 점등 포함
            return "DONE", "투입 및 1차 검사 완료"

        if command == "FLIP":
            print(" 🤖 [동작] 2차 뒤집기 및 검사 시작...")
            task_flip()
            inspection_second() # 조명 1초 점등 포함
            return "DONE", "뒤집기 및 2차 검사 완료"

        if command == "BRINGOUT":
            print(" 🤖 [동작] 검사 완료품 회수 시작...")
            task_bringout()
            return "DONE", "회수 완료"

        print(f" ❓ 알 수 없는 명령: {command}")
        return "ERROR", f"{command} 명령을 찾을 수 없음"

    except Exception as e:
        print(f" ❌ [오류] {command} 실행 실패: {e}")
        return "ERROR", f"{command} 실행 실패: {e}"


def run_client():
    print(f"[RPi 근육] NUC 서버({HOST}:{PORT})에 연결을 시도합니다...")
    
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.connect((HOST, PORT))
            print("[RPi 근육] 🟢 NUC 서버에 연결 성공! 명령을 대기합니다.\n")
            
            # 버퍼 역할을 할 파일 객체 생성
            fd = s.makefile('r')
            
            while True:
                data = fd.readline()
                if not data:
                    print("[RPi 근육] 🔴 NUC 서버가 닫혔습니다.")
                    break
                
                msg = json.loads(data)
                command = msg.get("command", "")
                print("-" * 40)
                print(f"[RPi 수신] ➔ 명령: {command}")
                
                # NUC로부터 받은 명령에 따라 실제 깃허브 함수 실행
                status, reply_msg = handle_command(command)
                if status == "DONE":
                    print(" 🤖 [동작] 로봇팔 이동 및 조명 제어 완료!")

                # NUC에게 동작 종료(DONE) 또는 실패(ERROR) 회신
                response = {
                    "status": status,
                    "message": reply_msg
                }
                print(f"[RPi 송신] ⬅️ NUC로 {status} 보고 전송")
                s.sendall((json.dumps(response) + "\n").encode('utf-8'))
                
        except ConnectionRefusedError:
            print("[RPi 근육] 🔴 연결 실패! NUC 쪽에 gui_server.py가 켜져 있는지 확인하세요.")

if __name__ == "__main__":
    run_client()
