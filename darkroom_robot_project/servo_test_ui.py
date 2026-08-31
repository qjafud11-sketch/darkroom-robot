"""서보만 테스트. 새 아두이노(CH340)에 0 / 90만 보낸다. 팔·조명은 안 건드린다."""
import threading
import tkinter as tk
from tkinter import StringVar

from arduino_link import resolve_servo_port, send_servo
from servo import ANGLE_180, ANGLE_HOME


class ServoTestUi:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("서보 테스트")
        self.root.geometry("400x280")
        self.port = resolve_servo_port()
        self.status = StringVar(
            value=f"포트 {self.port}\n버튼을 누르면 서보 보드에 각도를 보냅니다. 팔·조명은 안 움직입니다."
        )
        self.angle = StringVar(value="180")
        self.busy = False

        tk.Label(self.root, text="서보만 테스트 (CH340 아두이노 · D7 · SG90)", font=("Arial", 12, "bold")).pack(pady=(14, 4))
        tk.Label(
            self.root,
            text="주황=서보보드 D7  갈=GND  빨강=별도 5V   조명 보드 아님",
            fg="#555",
        ).pack()

        row = tk.Frame(self.root)
        row.pack(pady=12)
        tk.Button(row, text="원위치 18°", width=12, height=2, command=lambda: self._go(ANGLE_HOME)).pack(side="left", padx=8)
        tk.Button(row, text="180°", width=12, height=2, bg="#fde68a", command=lambda: self._go(ANGLE_180)).pack(side="left", padx=8)

        custom = tk.Frame(self.root)
        custom.pack(pady=6)
        tk.Label(custom, text="각도").pack(side="left")
        tk.Entry(custom, textvariable=self.angle, width=6).pack(side="left", padx=8)
        tk.Button(custom, text="보내기", command=self._go_custom).pack(side="left")

        tk.Label(self.root, textvariable=self.status, wraplength=360, justify="left").pack(pady=12, padx=12)

    def _go(self, command):
        if self.busy:
            return
        self.busy = True
        self.status.set(f"보내는 중... {command}")

        def worker():
            try:
                reply = send_servo(str(command), timeout=3.0, must_reply=True)
                extra = reply or "응답 없음"
                self.status.set(f"완료  {command}°   {extra}")
            except Exception as exc:
                self.status.set(f"실패: {exc}")
            finally:
                self.busy = False

        threading.Thread(target=worker, daemon=True).start()

    def _go_custom(self):
        try:
            value = int(self.angle.get())
        except ValueError:
            self.status.set("각도는 숫자로 입력하세요.")
            return
        if value < 0 or value > 180:
            self.status.set("각도는 0~180입니다.")
            return
        self._go(str(value))

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    ServoTestUi().run()
