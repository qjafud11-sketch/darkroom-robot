import socket
import json
import time
import tkinter as tk
import threading

HOST = '0.0.0.0' # 모든 라즈베리파이 접속 허용
PORT = 8585
conn = None

def server_listener():
    """백그라운드에서 라즈베리파이 접속을 기다리는 서버 쓰레드"""
    global conn
    print(f"[NUC] GUI 서버 구동 중... 포트 {PORT}에서 대기합니다.")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen()
        
        while True:
            c, addr = s.accept()
            print(f"\n[NUC] 🟢 라즈베리파이({addr})가 연결되었습니다!")
            conn = c

def send_command_sync(cmd):
    """라즈베리파이에 명령을 보내고 완료 응답(DONE)이 올 때까지 무한 대기"""
    if conn is None:
        print("[NUC] 🔴 라즈베리파이가 아직 연결되지 않았습니다!")
        return False
        
    msg = {"command": cmd, "timestamp": time.time()}
    try:
        conn.sendall((json.dumps(msg) + "\n").encode('utf-8'))
        print(f"[NUC] ➡️ 전송: {cmd}")
        
        # 라즈베리파이가 로봇팔 동작을 완전히 마칠 때까지 대기
        data = conn.recv(1024)
        if not data:
            print("[NUC] 🔴 연결 끊김!")
            return False
        resp = json.loads(data.decode('utf-8'))
        print(f"[NUC] ⬅️ 수신: {resp.get('status')} - {resp.get('message')}")
        return True
    except Exception as e:
        print(f"[NUC] 🔴 통신 에러: {e}")
        return False

def on_btn_click(action):
    """버튼 클릭 시 GUI가 멈추지 않도록 별도 쓰레드에서 실행"""
    threading.Thread(target=send_command_sync, args=(action,), daemon=True).start()

def run_full_sequence():
    """투입부터 회수까지 자동 실행"""
    def task():
        print("\n🚀 [자동 시퀀스] 전체 동작을 시작합니다...\n")
        sequence = ["INSERT", "FLIP", "BRINGOUT"]
        for step in sequence:
            success = send_command_sync(step)
            if not success:
                print("❌ 동작 실패 또는 연결 끊김으로 중단합니다.")
                break
            time.sleep(1.0) # 동작 간 부드러운 흐름을 위해 약간 대기
        print("\n✅ [자동 시퀀스] 모든 동작이 완료되었습니다!\n")
        
    threading.Thread(target=task, daemon=True).start()

# ================= GUI 화면 구성 =================
root = tk.Tk()
root.title("NUC 비전 검사 원격 컨트롤러")
root.geometry("400x350")

# 서버 수신 쓰레드 백그라운드 시작
threading.Thread(target=server_listener, daemon=True).start()

tk.Label(root, text="비전 검사 3단계 핵심 시퀀스", font=("Arial", 14, "bold")).pack(pady=10)
tk.Label(root, text="(라즈베리파이 연결 대기 중... 터미널 확인)", font=("Arial", 10)).pack(pady=5)

# 3단계 액션 버튼
actions = [
    ("1. 투입 및 1차 검사 (조명 연동)", "INSERT"),
    ("2. 뒤집기 및 2차 검사 (조명 연동)", "FLIP"),
    ("3. 검사 완료 및 회수", "BRINGOUT"),
]

for label, cmd in actions:
    btn = tk.Button(root, text=label, width=35, height=2, command=lambda c=cmd: on_btn_click(c))
    btn.pack(pady=5)

tk.Label(root, text="------------------------------------").pack(pady=5)

# 전체 자동 실행 버튼
auto_btn = tk.Button(root, text="🚀 전체 시퀀스 논스톱 실행 🚀", width=35, height=2, bg="lightblue", font=("Arial", 10, "bold"), command=run_full_sequence)
auto_btn.pack(pady=5)

# GUI 실행
root.mainloop()
