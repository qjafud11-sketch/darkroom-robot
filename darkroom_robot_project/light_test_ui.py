"""조명 밝기 테스트. FTDI 아두이노만 연다. 서보 보드는 안 건드린다."""
import threading
import tkinter as tk
from tkinter import StringVar

from arduino_link import resolve_port, send_ok


class LightTestUi:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("조명 테스트")
        self.root.geometry("400x260")

        self.port = resolve_port()
        self.brightness = StringVar(value="30")
        self.status = StringVar(
            value=f"포트 {self.port}\n켜기를 누르면 조명 보드에 B:밝기를 보냅니다. 서보는 안 움직입니다."
        )
        self.busy = False

        tk.Label(self.root, text="조명만 테스트 (FTDI 아두이노 · D8 · NeoPixel)", font=("Arial", 12, "bold")).pack(pady=(14, 4))
        tk.Label(self.root, text="켜기 = B:밝기    끄기 = OFF    검사 기본 밝기 30", fg="#555").pack()

        row = tk.Frame(self.root)
        row.pack(pady=12)
        tk.Label(row, text="밝기").pack(side="left")
        tk.Entry(row, textvariable=self.brightness, width=6).pack(side="left", padx=8)
        tk.Button(row, text="켜기", width=10, height=2, bg="#fde68a", command=self.turn_on).pack(side="left", padx=8)
        tk.Button(row, text="끄기", width=10, height=2, command=self.turn_off).pack(side="left", padx=8)

        tk.Label(self.root, textvariable=self.status, wraplength=360, justify="left").pack(pady=12, padx=12)

    def _send(self, command, ok_message):
        if self.busy:
            return
        self.busy = True
        self.status.set(f"보내는 중... {command}")

        def worker():
            try:
                reply = send_ok(command, timeout=3.0, must_reply=True)
                extra = reply or "응답 없음"
                self.status.set(f"{ok_message}   {extra}")
            except Exception as exc:
                self.status.set(f"실패: {exc}")
            finally:
                self.busy = False

        threading.Thread(target=worker, daemon=True).start()

    def turn_on(self):
        try:
            value = int(self.brightness.get())
        except ValueError:
            self.status.set("밝기는 숫자로 입력하세요.")
            return
        value = max(0, min(80, value))
        self._send(f"B:{value}", f"켜짐 B:{value}")

    def turn_off(self):
        self._send("OFF", "꺼짐")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    LightTestUi().run()
