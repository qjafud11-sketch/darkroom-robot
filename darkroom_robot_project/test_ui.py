import json
import socket
import threading
from datetime import datetime
from tkinter import (
    BooleanVar,
    END,
    HORIZONTAL,
    LEFT,
    RIGHT,
    Button,
    Checkbutton,
    Entry,
    Frame,
    IntVar,
    Label,
    LabelFrame,
    Scale,
    StringVar,
    Text,
    Tk,
)

from skills import (
    SCALE_MAX,
    SCALE_MIN,
    set_grip_wait,
    set_speed_scale,
    set_wait,
    task_bringout,
    task_flip,
    task_grip,
    task_insert,
    task_sort_ng,
    task_sort_ok,
)


TASKS = {
    "grip": ("0 샘플 집기", task_grip),
    "insert": ("1차 샘플 넣기", task_insert),
    "flip": ("2차 샘플 뒤집기", task_flip),
    "bringout": ("3차 샘플 꺼내기", task_bringout),
    "sort_ok": ("4차 양품 분류", task_sort_ok),
    "sort_ng": ("4차 불량 분류", task_sort_ng),
}


class CommClient:
    """추후 조명·카메라·아두이노 연동용 JSON 송신기."""

    def __init__(self, host_getter, port_getter, enabled_getter, logger):
        self.host_getter = host_getter
        self.port_getter = port_getter
        self.enabled_getter = enabled_getter
        self.logger = logger

    def send(self, event, payload=None):
        if not self.enabled_getter():
            return

        message = {
            "event": event,
            "payload": payload or {},
            "sent_at": datetime.now().isoformat(timespec="seconds"),
        }

        host = self.host_getter().strip() or "127.0.0.1"
        port = int(self.port_getter())

        try:
            with socket.create_connection((host, port), timeout=1.0) as sock:
                sock.sendall((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            self.logger(f"[통신] {event} -> {host}:{port}")
        except Exception as exc:
            self.logger(f"[통신오류] {event} 전송 실패: {exc}")


class TestUi:
    def __init__(self):
        self.root = Tk()
        self.root.title("암실 로봇 테스트 UI")
        self.root.geometry("760x520")

        self.is_running = False
        self.wait_var = StringVar(value="0")
        self.grip_wait_var = StringVar(value="0.5")
        self.scale_var = IntVar(value=100)
        self.scale_text = StringVar(value="100")
        self.host_var = StringVar(value="127.0.0.1")
        self.port_var = StringVar(value="9000")
        self.comm_enabled = BooleanVar(value=False)

        self.buttons = {}
        self.log_text = None

        self._build_top_controls()
        self._build_speed_control()
        self._build_task_buttons()
        self._build_log_area()

        self.comm = CommClient(
            host_getter=self.host_var.get,
            port_getter=self.port_var.get,
            enabled_getter=self.comm_enabled.get,
            logger=self.log,
        )

    def _build_top_controls(self):
        top = Frame(self.root, padx=12, pady=12)
        top.pack(fill="x")

        wait_box = LabelFrame(top, text="실행 설정", padx=10, pady=8)
        wait_box.pack(side=LEFT, fill="x", expand=True, padx=(0, 8))

        Label(wait_box, text="추가 대기(초)").pack(side=LEFT)
        Entry(wait_box, textvariable=self.wait_var, width=6).pack(side=LEFT, padx=(8, 0))

        Label(wait_box, text="그리퍼 대기(초)").pack(side=LEFT, padx=(16, 0))
        Entry(wait_box, textvariable=self.grip_wait_var, width=6).pack(side=LEFT, padx=(8, 0))

        comm_box = LabelFrame(top, text="통신 훅", padx=10, pady=8)
        comm_box.pack(side=RIGHT, fill="x")

        Checkbutton(comm_box, text="통신 사용", variable=self.comm_enabled).pack(side=LEFT)
        Label(comm_box, text="HOST").pack(side=LEFT, padx=(10, 4))
        Entry(comm_box, textvariable=self.host_var, width=12).pack(side=LEFT)
        Label(comm_box, text="PORT").pack(side=LEFT, padx=(10, 4))
        Entry(comm_box, textvariable=self.port_var, width=7).pack(side=LEFT)

    def _build_speed_control(self):
        box = LabelFrame(self.root, text="속도 배율", padx=12, pady=8)
        box.pack(fill="x", padx=12, pady=(0, 8))

        low, high = int(SCALE_MIN * 100), int(SCALE_MAX * 100)

        Scale(
            box,
            from_=low,
            to=high,
            resolution=10,
            orient=HORIZONTAL,
            variable=self.scale_var,
            command=self._on_slider,
            showvalue=False,
            length=520,
        ).pack(side=LEFT, fill="x", expand=True)

        Entry(box, textvariable=self.scale_text, width=6).pack(side=LEFT, padx=(12, 2))
        Label(box, text="%").pack(side=LEFT)
        self.scale_text.trace_add("write", self._on_typed)

        Label(
            box,
            text=f"{low}~{high}%  ·  관절 비율은 그대로 유지",
            fg="#666",
        ).pack(side=LEFT, padx=(12, 0))

    def _on_slider(self, value):
        if self.scale_text.get() != str(int(float(value))):
            self.scale_text.set(str(int(float(value))))

    def _on_typed(self, *_):
        try:
            typed = int(float(self.scale_text.get()))
        except ValueError:
            return
        if self.scale_var.get() != typed:
            self.scale_var.set(typed)

    def _build_task_buttons(self):
        task_box = LabelFrame(
            self.root,
            text="테스트 버튼",
            padx=12,
            pady=12,
        )
        task_box.pack(fill="x", padx=12, pady=(0, 12))

        for task_key, (label, _) in TASKS.items():
            btn = Button(
                task_box,
                text=label,
                width=20,
                height=2,
                command=lambda key=task_key: self.run_task(key),
            )
            btn.pack(side=LEFT, padx=8)
            self.buttons[task_key] = btn

    def _build_log_area(self):
        log_box = LabelFrame(self.root, text="실행 로그", padx=12, pady=12)
        log_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.log_text = Text(log_box, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log("UI 준비 완료")
        self.log("버튼을 누르면 해당 작업 전체를 순서대로 실행합니다.")

    def set_buttons_state(self, enabled):
        state = "normal" if enabled else "disabled"
        for btn in self.buttons.values():
            btn.config(state=state)

    def log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(END, f"[{timestamp}] {message}\n")
        self.log_text.see(END)

    def run_task(self, task_key):
        if self.is_running:
            self.log("이미 다른 작업이 실행 중입니다.")
            return

        try:
            wait_seconds = float(self.wait_var.get())
        except ValueError:
            self.log("추가 대기값이 숫자가 아닙니다.")
            return

        try:
            grip_seconds = float(self.grip_wait_var.get())
        except ValueError:
            self.log("그리퍼 대기값이 숫자가 아닙니다.")
            return

        try:
            scale_percent = float(self.scale_text.get())
        except ValueError:
            self.log("속도 배율이 숫자가 아닙니다.")
            return

        self.is_running = True
        self.set_buttons_state(False)

        task_name, task_fn = TASKS[task_key]

        def worker():
            try:
                set_wait(wait_seconds)
                set_grip_wait(grip_seconds)
                applied = set_speed_scale(scale_percent)
                dwell = "도착 즉시 다음 스텝" if wait_seconds <= 0 else f"도착 후 {wait_seconds}초 추가 대기"
                self.log(f"{task_name} 시작 (속도 {applied:.0f}%, {dwell}, 그리퍼 {grip_seconds}초)")
                self.comm.send("task.start", {"task": task_key, "label": task_name})
                task_fn()
                self.comm.send("task.done", {"task": task_key, "label": task_name})
                self.log(f"{task_name} 완료")
            except Exception as exc:
                self.comm.send("task.error", {"task": task_key, "error": str(exc)})
                self.log(f"{task_name} 실패: {exc}")
            finally:
                self.root.after(0, self._finish_task)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_task(self):
        self.is_running = False
        self.set_buttons_state(True)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    TestUi().run()
