import socket
import json
import time

# 깃허브 로봇팔 코드에서 실제 동작 함수들을 임포트합니다.
from skills import task_insert, task_flip, task_bringout
from inspection import inspection_first, inspection_second

HOST = '127.0.0.1' # 로컬 테스트 시 127.0.0.1, 실제 NUC(서버)가 다른 PC라면 해당 IP로 변경
PORT = 8585

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
                if command == "INSERT":
                    print(" 🤖 [동작] 1차 투입 및 검사 시작...")
                    task_insert()
                    inspection_first() # 조명 1초 점등 포함
                    reply_msg = "투입 및 1차 검사 완료"
                    
                elif command == "FLIP":
                    print(" 🤖 [동작] 2차 뒤집기 및 검사 시작...")
                    task_flip()
                    inspection_second() # 조명 1초 점등 포함
                    reply_msg = "뒤집기 및 2차 검사 완료"
                    
                elif command == "BRINGOUT":
                    print(" 🤖 [동작] 검사 완료품 회수 시작...")
                    task_bringout()
                    reply_msg = "회수 완료"
                    
                else:
                    print(f" ❓ 알 수 없는 명령: {command}")
                    reply_msg = f"에러: {command} 명령을 찾을 수 없음"
                
                print(" 🤖 [동작] 로봇팔 이동 및 조명 제어 완료!")
                
                # NUC에게 동작 완전 종료(DONE) 회신
                response = {
                    "status": "DONE",
                    "message": reply_msg
                }
                print(f"[RPi 송신] ⬅️ NUC로 동작 완료(DONE) 보고 전송")
                s.sendall((json.dumps(response) + "\n").encode('utf-8'))
                
        except ConnectionRefusedError:
            print("[RPi 근육] 🔴 연결 실패! NUC 쪽에 gui_server.py가 켜져 있는지 확인하세요.")

if __name__ == "__main__":
    run_client()
