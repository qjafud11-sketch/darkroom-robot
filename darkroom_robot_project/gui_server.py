import json
import queue
import socket
import threading
import time
import tkinter as tk
from io import BytesIO
from tkinter import DISABLED, END, NORMAL, WORD, Frame, Label, Text

from PIL import Image, ImageTk

from camera import CAMERAS, CAPTURE_DIR, LiveCamera, grab_stills, start_capture_server
from hw_ports import report as report_ports

HOST = "0.0.0.0"
PORT = 8585
conn = None
conn_file = None
is_busy = False
buttons = []
root = None
link_var = None
log_box = None
camera_wall = None


def ui_log(text):
    """창 아래 통신 기록. 워커 쓰레드에서도 호출 가능."""
    def append():
        if log_box is None:
            return
        log_box.config(state=NORMAL)
        log_box.insert(END, text + "\n")
        log_box.see(END)
        log_box.config(state=DISABLED)

    if root is None:
        print(text)
        return
    root.after(0, append)


def set_link(text):
    if root is None or link_var is None:
        return
    root.after(0, lambda: link_var.set(text))


def server_listener():
    """실행기(robot_client) 접속을 기다린다."""
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
                    conn.close()
                except Exception:
                    pass
            print(f"\n[NUC] 실행기 연결: {addr}")
            conn, conn_file = c, c.makefile("r")
            set_link(f"실행기 연결됨  {addr[0]}")
            ui_log(f"연결됨 {addr[0]}:{addr[1]}")


def send_command_sync(cmd):
    """한 명령을 보내고 DONE이 올 때까지 기다린다."""
    if conn is None:
        ui_log("실행기가 아직 연결되지 않았습니다. robot_client.py를 켜세요.")
        return False

    msg = {"command": cmd, "timestamp": time.time()}
    try:
        conn.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        ui_log(f"전송  {cmd}")
        print(f"[NUC] 전송: {cmd}")

        line = conn_file.readline()
        if not line:
            set_link("실행기 연결 끊김")
            ui_log("연결 끊김")
            return False
        resp = json.loads(line)
        status = resp.get("status")
        message = resp.get("message", "")
        ui_log(f"수신  {status}  {message}")
        print(f"[NUC] 수신: {status} - {message}")
        return status == "DONE"
    except Exception as e:
        ui_log(f"통신 에러  {e}")
        print(f"[NUC] 통신 에러: {e}")
        return False


def send_sequence(commands):
    """명령 목록을 순서대로 보낸다. 하나라도 DONE이 아니면 중단."""
    for cmd in commands:
        if not send_command_sync(cmd):
            ui_log("중단 — 다음 명령을 보내지 않습니다.")
            return False
    return True


def set_buttons_enabled(enabled):
    state = NORMAL if enabled else DISABLED
    for btn in buttons:
        btn.config(state=state)


def run_exclusive(job, *args):
    global is_busy
    if is_busy:
        ui_log("이미 동작 중입니다.")
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
    run_exclusive(send_command_sync, action)


def run_ping():
    """팔·조명을 건드리지 않고 UI ↔ 실행기만 확인."""
    run_exclusive(send_command_sync, "PING")


def run_insert_inspect():
    run_exclusive(send_sequence, ["INSERT", "INSPECT_1"])


def run_flip_inspect():
    """뒤집기 후 2차 검사(서보 180 → 조명·촬영 → 서보 18)."""
    run_exclusive(send_sequence, ["FLIP", "INSPECT_2"])


def run_full_sequence():
    def task():
        ui_log("자동 시퀀스 시작")
        sequence = [
            "INSERT",
            "INSPECT_1",
            "GAP",
            "FLIP",
            "INSPECT_2",
            "GAP",
            "BRINGOUT",
        ]
        if send_sequence(sequence):
            ui_log("자동 시퀀스 완료")

    run_exclusive(task)


class CameraPicker:
    """카메라 1~4 중 고른 한 대만 켠다. 동시에 여러 대를 열면 USB가 죽는다."""

    def __init__(self, parent):
        self.selected = None
        self.lock = threading.Lock()
        self.paused = False
        self.stopped = False
        self.photo = None
        self.live = LiveCamera(width=800, height=600, fps=15)
        self.cam_buttons = {}

        top = Frame(parent)
        top.pack(fill="x", padx=8, pady=(4, 0))
        for cam in CAMERAS:
            connected = bool(cam.get("device"))
            btn = tk.Button(
                top,
                text=cam["name"] if connected else f"{cam['name']} 미연결",
                width=12,
                height=2,
                state=NORMAL if connected else DISABLED,
                command=lambda c=cam: self.select(c),
            )
            btn.pack(side="left", padx=4)
            self.cam_buttons[cam["id"]] = btn

        self.status = tk.StringVar(value="카메라 번호를 누르면 그 대만 켭니다.")
        tk.Label(parent, textvariable=self.status, fg="#555").pack(anchor="w", padx=12, pady=(4, 0))

        self.preview = Label(parent, text="대기 — 1~4 중 한 대만 선택", bg="#222", fg="#ddd")
        self.preview.pack(fill="both", expand=True, padx=8, pady=8)

    def select(self, cam):
        self.selected = cam
        for cam_id, btn in self.cam_buttons.items():
            if cam_id == cam["id"]:
                btn.config(relief="sunken", bg="#fde68a")
            else:
                btn.config(relief="raised", bg=self.preview.master.cget("bg"))
        if not cam.get("device"):
            self.live.stop()
            self.status.set(f"{cam['name']} 장치가 없습니다.")
            self.preview.config(image="", text="미연결")
            return
        try:
            from camera_calib import apply_saved
            apply_saved(cam["id"], cam["device"])
        except Exception:
            pass
        self.live.start(cam["device"])
        self.status.set(f"{cam['name']}  {cam['device']}  ·  이 대만 스트림")

    def start(self):
        self.stopped = False
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.stopped = True
        self.live.stop()

    def _show(self, png=None, text=None):
        def apply():
            if png:
                try:
                    img = Image.open(BytesIO(png)).convert("RGB")
                    img.thumbnail((960, 720))
                    photo = ImageTk.PhotoImage(img)
                    self.photo = photo
                    self.preview.config(image=photo, text="")
                except Exception as exc:
                    self.preview.config(image="", text=f"표시 실패: {exc}")
            elif text:
                self.preview.config(image="", text=text)
        if root is not None:
            root.after(0, apply)

    def _loop(self):
        while not self.stopped:
            if self.paused:
                time.sleep(0.1)
                continue
            if self.live.last_error:
                err = self.live.last_error
                self.live.last_error = ""
                self._show(text=err)
            try:
                png = self.live.frames.get(timeout=0.2)
            except queue.Empty:
                continue
            if self.stopped or self.paused:
                continue
            self._show(png=png)

    def grab(self, label):
        """프리뷰 스트림을 멈추고 고해상도 한 장을 저장한 뒤 다시 켠다."""
        with self.lock:
            self.paused = True
            cam = self.selected
            device = cam.get("device") if cam else None
            self.live.stop()
            time.sleep(0.25)
            try:
                manifest = grab_stills(label)
            finally:
                if device and not self.stopped:
                    try:
                        from camera_calib import apply_saved
                        apply_saved(cam["id"], device)
                    except Exception:
                        pass
                    self.live.start(device)
                self.paused = False
            ui_log(f"촬영 저장  {manifest['folder']}")
            return manifest


def build_ui():
    global root, link_var, log_box, camera_wall

    root = tk.Tk()
    root.title("NUC 비전 검사 원격 컨트롤러")
    root.geometry("1100x720")

    body = Frame(root)
    body.pack(fill="both", expand=True)

    left = Frame(body, width=380)
    left.pack(side="left", fill="y", padx=8, pady=8)
    left.pack_propagate(False)

    right = Frame(body)
    right.pack(side="left", fill="both", expand=True)

    tk.Label(left, text="비전 검사 원격 컨트롤러", font=("Arial", 14, "bold")).pack(pady=(8, 4))

    link_var = tk.StringVar(value="실행기 연결 대기 중...  robot_client.py를 켜세요")
    tk.Label(left, textvariable=link_var, font=("Arial", 10), fg="#555", wraplength=340).pack(pady=4)

    ping_btn = tk.Button(
        left,
        text="통신 확인 (팔·조명 안 움직임)",
        width=32,
        height=2,
        command=run_ping,
    )
    ping_btn.pack(pady=(8, 4))
    buttons.append(ping_btn)

    tk.Label(left, text="아래는 실제 동작").pack(pady=(8, 2))

    btn_insert = tk.Button(left, text="1. 투입 후 1차 검사", width=32, height=2, command=run_insert_inspect)
    btn_insert.pack(pady=3)
    buttons.append(btn_insert)

    btn_flip = tk.Button(left, text="2. 뒤집기 후 2차 검사 (서보 180°)", width=32, height=2, command=run_flip_inspect)
    btn_flip.pack(pady=3)
    buttons.append(btn_flip)

    btn_out = tk.Button(left, text="3. 회수", width=32, height=2, command=lambda: on_btn_click("BRINGOUT"))
    btn_out.pack(pady=3)
    buttons.append(btn_out)

    auto_btn = tk.Button(
        left,
        text="전체 시퀀스 논스톱",
        width=32,
        height=2,
        bg="lightblue",
        font=("Arial", 10, "bold"),
        command=run_full_sequence,
    )
    auto_btn.pack(pady=6)
    buttons.append(auto_btn)

    tk.Label(left, text="통신 기록").pack(pady=(8, 2))
    log_box = Text(left, height=14, width=42, wrap=WORD, state=DISABLED)
    log_box.pack(padx=4, pady=(0, 8), fill="both", expand=True)

    tk.Label(right, text="카메라 1~4 · 한 대만 선택해서 보기", font=("Arial", 11, "bold")).pack(pady=(8, 0))
    camera_wall = CameraPicker(right)


def on_close():
    if camera_wall is not None:
        camera_wall.stop()
    root.destroy()


if __name__ == "__main__":
    build_ui()
    root.protocol("WM_DELETE_WINDOW", on_close)
    threading.Thread(target=server_listener, daemon=True).start()
    start_capture_server(lambda label: camera_wall.grab(label))
    camera_wall.start()
    ui_log(f"서버 대기 포트 {PORT}")
    ui_log(f"촬영 저장  {CAPTURE_DIR}")
    report_ports(ui_log)
    root.mainloop()
