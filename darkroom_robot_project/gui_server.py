import socket
import json
import time
import tkinter as tk
import threading

HOST = '0.0.0.0' # 모든 라즈베리파이 접속 허용
PORT = 8585
conn = None
conn_file = None  # 줄 단위 수신용 — 클라이언트가 json+"\n"로 보내므로 readline으로 맞춤
is_busy = False   # 동작 중 버튼 재클릭 방지
buttons = []

def server_listener():
    """백그라운드에서 라즈베리파이 접속을 기다리는 서버 쓰레드"""
    global conn, conn_file
    print(f"[NUC] GUI 서버 구동 중... 포트 {PORT}에서 대기합니다.")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen()
        
        while True:
            c, addr = s.accept()
            if conn is not None:
                try:
                    conn.close()  # 이전 접속 정리 — 죽은 소켓에 계속 쓰지 않게
                except Exception:
                    pass
            print(f"\n[NUC] 🟢 라즈베리파이({addr})가 연결되었습니다!")
            conn, conn_file = c, c.makefile('r')

def send_command_sync(cmd):
    """라즈베리파이에 명령을 보내고 완료 응답(DONE)이 올 때까지 무한 대기"""
    if conn is None:
        print("[NUC] 🔴 라즈베리파이가 아직 연결되지 않았습니다!")
        return False
        
    msg = {"command": cmd, "timestamp": time.time()}
    try:
        conn.sendall((json.dumps(msg) + "\n").encode('utf-8'))
        print(f"[NUC] ➡️ 전송: {cmd}")
        
        # 라즈베리파이가 로봇팔 동작을 완전히 마칠 때까지 대기.
        # 응답이 줄 단위라 readline으로 읽어야 두 개가 붙어 와도 안 깨진다
        line = conn_file.readline()
        if not line:
            print("[NUC] 🔴 연결 끊김!")
            return False
        resp = json.loads(line)
        status = resp.get('status')
        print(f"[NUC] ⬅️ 수신: {status} - {resp.get('message')}")
        return status == "DONE"
    except Exception as e:
        print(f"[NUC] 🔴 통신 에러: {e}")
        return False

def set_buttons_enabled(enabled):
    """동작 중에는 버튼을 잠근다"""
    state = tk.NORMAL if enabled else tk.DISABLED
    for btn in buttons:
        btn.config(state=state)

def run_exclusive(job, *args):
    """한 번에 하나만 실행. 명령이 겹쳐 로봇이 순서를 건너뛰는 것을 막는다"""
    global is_busy
    if is_busy:
        print("[NUC] ⚠️ 이미 동작이 진행 중입니다. 완료 후 다시 눌러주세요.")
        return

    is_busy = True
    set_buttons_enabled(False)

    def worker():
        global is_busy
        try:
            job(*args)
        finally:
            is_busy = False
            root.after(0, lambda: set_buttons_enabled(True))

    threading.Thread(target=worker, daemon=True).start()

def on_btn_click(action):
    """버튼 클릭 시 GUI가 멈추지 않도록 별도 쓰레드에서 실행"""
    run_exclusive(send_command_sync, action)

def run_full_sequence():
    """투입부터 회수까지 자동 실행"""
    def task():
        print("\n🚀 [자동 시퀀스] 전체 동작을 시작합니다...\n")
        sequence = ["INSERT", "FLIP", "BRINGOUT"]
        for step in sequence:
            success = send_command_sync(step)
            if not success:
                print("❌ 동작 실패 또는 연결 끊김으로 중단합니다.")
                return
            time.sleep(1.0) # 동작 간 부드러운 흐름을 위해 약간 대기
        print("\n✅ [자동 시퀀스] 모든 동작이 완료되었습니다!\n")

    run_exclusive(task)

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
    buttons.append(btn)

tk.Label(root, text="------------------------------------").pack(pady=5)

# 전체 자동 실행 버튼
auto_btn = tk.Button(root, text="🚀 전체 시퀀스 논스톱 실행 🚀", width=35, height=2, bg="lightblue", font=("Arial", 10, "bold"), command=run_full_sequence)
auto_btn.pack(pady=5)
buttons.append(auto_btn)

# GUI 실행
root.mainloop()
