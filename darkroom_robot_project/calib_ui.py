"""카메라 캘리브레이션 UI.

4대 중 하나를 골라 밝기·대비·노출·소프트웨어 선명을 맞춘다.
C270은 고정 초점이라 초점 슬라이더가 없고, 선명·대비 필터로 가장자리를 살린다.
USB 한계로 프리뷰는 고른 카메라 한 대만 연다.
조명은 이 창에서 ON/OFF 한다 (FTDI, 검사와 동일 B:밝기).
원격 UI와 동시에 켜지 말 것 — 같은 카메라를 두고 싸운다.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from io import BytesIO
from tkinter import DISABLED, NORMAL, Frame, Label, Canvas, Scrollbar

from PIL import Image, ImageTk

from arduino_link import resolve_port, send_ok
from camera import CAMERAS, LiveCamera
from camera_calib import (
    FILTER_DEFAULTS,
    GROUPS,
    apply_filters,
    filters_active,
    label_of,
    list_controls,
    load_camera,
    load_filters,
    save_camera,
    set_controls,
    sharpness_score,
)


class CalibUi:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("카메라 캘리브레이션")
        self.root.geometry("1700x1050")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.selected = None
        self.cam_buttons = {}
        self.controls = []
        self.widgets = {}
        self.filter_values = dict(FILTER_DEFAULTS)
        self.filter_preview = tk.BooleanVar(value=True)
        self._preview_filters = True
        self.score_var = tk.StringVar(value="선명도 점수  —")
        self.photo = None
        self.live = LiveCamera(width=1280, height=960, fps=15)
        self.busy = False
        self.light_busy = False
        self._pending = None
        self.stopped = False
        self.light_brightness = tk.StringVar(value="30")
        self.status = tk.StringVar(value="카메라 1~4 중 한 대를 누르면 그 대만 켭니다.")

        self._build()
        threading.Thread(target=self._preview_loop, daemon=True).start()

    def _build(self):
        top = Frame(self.root)
        top.pack(fill="x", padx=10, pady=(10, 4))
        tk.Label(top, text="카메라 캘리브레이션", font=("Arial", 14, "bold")).pack(side="left")
        tk.Label(
            top,
            text="한 대만 켜서 맞춥니다. 장치값·소프트웨어 선명은 검사 촬영에도 적용됩니다.",
            fg="#555",
        ).pack(side="left", padx=12)

        cams = Frame(self.root)
        cams.pack(fill="x", padx=10, pady=6)
        for cam in CAMERAS:
            connected = bool(cam.get("device"))
            text = cam["name"] if connected else f"{cam['name']}  ·  미연결"
            btn = tk.Button(
                cams,
                text=text,
                width=18,
                height=2,
                state=NORMAL if connected else DISABLED,
                command=lambda c=cam: self.select(c),
            )
            btn.pack(side="left", padx=4)
            self.cam_buttons[cam["id"]] = btn

        lights = Frame(self.root, bd=1, relief="groove")
        lights.pack(fill="x", padx=10, pady=(0, 6))
        tk.Label(lights, text="조명", font=("Arial", 10, "bold")).pack(side="left", padx=(8, 6), pady=8)
        tk.Label(lights, text="밝기").pack(side="left")
        tk.Entry(lights, textvariable=self.light_brightness, width=5).pack(side="left", padx=6)
        tk.Button(lights, text="켜기", width=8, height=1, bg="#fde68a", command=self.light_on).pack(side="left", padx=4)
        tk.Button(lights, text="끄기", width=8, height=1, command=self.light_off).pack(side="left", padx=4)
        tk.Label(
            lights,
            text=f"FTDI {resolve_port()}  ·  검사와 동일 B:밝기 / OFF",
            fg="#555",
        ).pack(side="left", padx=10)

        body = Frame(self.root)
        body.pack(fill="both", expand=True, padx=10, pady=4)

        preview_wrap = Frame(body, bd=1, relief="solid")
        preview_wrap.pack(side="left", fill="both", expand=True, padx=(0, 8))
        tk.Label(preview_wrap, text="고른 카메라만 켭니다", font=("Arial", 10, "bold")).pack(pady=(6, 0))
        self.preview = Label(preview_wrap, text="대기 — 1~4 중 한 대만 선택", bg="#222", fg="#ddd")
        self.preview.pack(fill="both", expand=True, padx=8, pady=8)

        right = Frame(body, width=420)
        right.pack(side="left", fill="y")
        right.pack_propagate(False)

        canvas = Canvas(right, highlightthickness=0, width=400)
        scroll = Scrollbar(right, orient="vertical", command=canvas.yview)
        self.ctrl_host = Frame(canvas)
        self.ctrl_host.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=self.ctrl_host, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        btns = Frame(self.root)
        btns.pack(fill="x", padx=10, pady=6)
        tk.Button(btns, text="이 카메라 저장", width=14, height=2, bg="#fde68a", command=self.save).pack(side="left", padx=4)
        tk.Button(btns, text="저장값 불러오기", width=14, height=2, command=self.reload_saved).pack(side="left", padx=4)
        tk.Button(btns, text="장치 기본값", width=14, height=2, command=self.reset_defaults).pack(side="left", padx=4)
        tk.Label(btns, textvariable=self.status, wraplength=620, justify="left").pack(side="left", padx=12)

    def select(self, cam):
        self.selected = cam
        for cam_id, btn in self.cam_buttons.items():
            if cam_id == cam["id"]:
                btn.config(relief="sunken", bg="#fde68a")
            else:
                default_bg = self.root.cget("bg")
                btn.config(relief="raised", bg=default_bg)
        if not cam.get("device"):
            self.live.stop()
            self.status.set(f"{cam['name']} 은 아직 장치가 없습니다.")
            self._clear_controls()
            self._show_text("미연결")
            return
        try:
            saved = load_camera(cam["id"])
            self.filter_values = load_filters(cam["id"])
            if saved:
                set_controls(cam["device"], saved)
            self._rebuild_controls()
            self.live.start(cam["device"])
            extra = "저장값 적용" if saved else "장치 현재값"
            self.status.set(f"{cam['name']}  {cam['device']}  ·  {extra}")
        except Exception as exc:
            self.live.stop()
            self.status.set(f"{cam['name']} 열기 실패: {exc}")

    def _clear_controls(self):
        for child in self.ctrl_host.winfo_children():
            child.destroy()
        self.widgets = {}
        self.controls = []

    def _rebuild_controls(self):
        self._clear_controls()
        device = self.selected["device"]
        self.controls = list_controls(device)
        by_name = {item["name"]: item for item in self.controls}
        used = set()

        self._add_filter_controls()

        for title, names in GROUPS:
            present = [by_name[name] for name in names if name in by_name]
            box = Frame(self.ctrl_host, bd=1, relief="groove")
            box.pack(fill="x", padx=4, pady=6)
            tk.Label(box, text=title, font=("Arial", 10, "bold")).pack(anchor="w", padx=8, pady=(6, 2))
            if not present:
                note = "이 카메라는 고정 초점입니다. 아래 소프트웨어 선명으로 가장자리를 살리면 됩니다."
                if title != "초점":
                    note = "이 카메라에는 해당 항목이 없습니다."
                tk.Label(box, text=note, fg="#666", wraplength=360, justify="left").pack(anchor="w", padx=8, pady=(0, 8))
                continue
            for item in present:
                self._add_control(box, item)
                used.add(item["name"])

        leftover = [item for item in self.controls if item["name"] not in used]
        if leftover:
            box = Frame(self.ctrl_host, bd=1, relief="groove")
            box.pack(fill="x", padx=4, pady=6)
            tk.Label(box, text="기타", font=("Arial", 10, "bold")).pack(anchor="w", padx=8, pady=(6, 2))
            for item in leftover:
                self._add_control(box, item)

    def _add_filter_controls(self):
        box = Frame(self.ctrl_host, bd=1, relief="groove")
        box.pack(fill="x", padx=4, pady=6)
        tk.Label(box, text="소프트웨어 보정", font=("Arial", 10, "bold")).pack(
            anchor="w", padx=8, pady=(6, 2)
        )
        tk.Label(
            box,
            text="초점을 못 바꿀 때 가장자리와 대비를 살립니다. 저장하면 검사 사진에도 들어갑니다.",
            fg="#666",
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 4))
        tk.Checkbutton(
            box,
            text="보정 미리보기 (끄면 원본과 비교)",
            variable=self.filter_preview,
            command=self._sync_preview_flag,
        ).pack(anchor="w", padx=8)
        tk.Label(box, textvariable=self.score_var).pack(anchor="w", padx=8, pady=(2, 6))

        specs = (
            ("unsharp", "소프트웨어 선명", 0, 250, 1, None),
            ("unsharp_radius", "선명 범위", 5, 40, 1, "radius"),
            ("local_contrast", "대비 보정", 0, 80, 1, None),
            ("denoise", "노이즈 완화", 0, 5, 1, None),
        )
        for name, label, lo, hi, step, kind in specs:
            row = Frame(box)
            row.pack(fill="x", padx=8, pady=3)
            tk.Label(row, text=label, width=16, anchor="w").pack(side="left")
            value = int(self.filter_values.get(name, FILTER_DEFAULTS[name]))
            shown = tk.Label(row, width=6)
            shown.pack(side="right")
            var = tk.IntVar(value=value)

            def _text(val, mode=kind):
                number = int(float(val))
                if mode == "radius":
                    return f"{number / 10:.1f}"
                return str(number)

            shown.config(text=_text(value))
            scale = tk.Scale(
                row,
                from_=lo,
                to=hi,
                resolution=step,
                orient="horizontal",
                showvalue=0,
                length=200,
                variable=var,
                command=lambda val, n=name, s=shown, t=_text: self._filter_slide(n, val, s, t),
            )
            scale.pack(side="left", fill="x", expand=True, padx=6)

    def _filter_slide(self, name, value, shown, fmt):
        number = int(float(value))
        shown.config(text=fmt(number))
        self.filter_values[name] = number

    def _sync_preview_flag(self):
        self._preview_filters = bool(self.filter_preview.get())

    def _add_control(self, parent, item):
        row = Frame(parent)
        row.pack(fill="x", padx=8, pady=3)
        tk.Label(row, text=label_of(item["name"]), width=16, anchor="w").pack(side="left")
        state = DISABLED if item["inactive"] else NORMAL
        kind = item["type"]

        if kind == "bool" or (kind == "int" and item["min"] == 0 and item["max"] == 1):
            var = tk.IntVar(value=int(item["value"]))
            widget = tk.Checkbutton(
                row,
                variable=var,
                command=lambda n=item["name"], v=var: self._change(n, v.get()),
            )
            widget.config(state=state)
            widget.pack(side="left")
            self.widgets[item["name"]] = {"var": var, "widget": widget, "kind": "bool"}
            return

        if kind == "menu" and item["menus"]:
            choices = item["menus"]
            var = tk.StringVar(value=f"{item['value']}: {choices.get(int(item['value']), '')}")
            display = [f"{key}: {label}" for key, label in sorted(choices.items())]
            menu = tk.OptionMenu(row, var, *display, command=lambda val, n=item["name"]: self._change_menu(n, val))
            menu.config(state=state, width=22)
            menu.pack(side="left")
            self.widgets[item["name"]] = {"var": var, "widget": menu, "kind": "menu"}
            return

        var = tk.IntVar(value=int(item["value"]))
        shown = tk.Label(row, text=str(int(item["value"])), width=6)
        shown.pack(side="right")
        scale = tk.Scale(
            row,
            from_=item["min"],
            to=item["max"],
            resolution=item["step"] or 1,
            orient="horizontal",
            showvalue=0,
            length=200,
            variable=var,
            state=state,
            command=lambda val, n=item["name"], s=shown: self._slide(n, val, s),
        )
        scale.pack(side="left", fill="x", expand=True, padx=6)
        scale.bind("<ButtonRelease-1>", lambda e, n=item["name"]: self._apply_now(n))
        self.widgets[item["name"]] = {"var": var, "widget": scale, "kind": "int", "shown": shown}

    def _slide(self, name, value, shown):
        shown.config(text=str(int(float(value))))

    def _change_menu(self, name, display):
        value = int(str(display).split(":", 1)[0])
        self._change(name, value)

    def _change(self, name, value):
        self._apply_now(name, int(value))

    def _collect(self):
        values = {}
        for name, slot in self.widgets.items():
            kind = slot["kind"]
            if kind == "menu":
                values[name] = int(str(slot["var"].get()).split(":", 1)[0])
            else:
                values[name] = int(slot["var"].get())
        return values

    def _apply_now(self, name=None, value=None):
        cam = self.selected
        if cam is None or not cam.get("device"):
            return
        values = self._collect()
        if name is not None and value is not None:
            values[name] = int(value)
        self._pending = values
        if self.busy:
            return
        self.busy = True
        device = cam["device"]

        def worker():
            try:
                while self._pending is not None:
                    payload = self._pending
                    self._pending = None
                    set_controls(device, payload)
                self.root.after(0, self._refresh_inactive)
            except Exception as exc:
                message = str(exc)
                self.root.after(0, lambda m=message: self.status.set(f"적용 실패: {m}"))
            finally:
                self.busy = False

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_inactive(self):
        cam = self.selected
        if cam is None or not cam.get("device"):
            return
        try:
            fresh = {item["name"]: item for item in list_controls(cam["device"])}
        except Exception:
            return
        for name, slot in self.widgets.items():
            item = fresh.get(name)
            if not item:
                continue
            state = DISABLED if item["inactive"] else NORMAL
            try:
                slot["widget"].config(state=state)
            except tk.TclError:
                pass

    def save(self):
        cam = self.selected
        if cam is None or not cam.get("device"):
            self.status.set("저장할 카메라가 없습니다.")
            return
        path = save_camera(
            cam["id"], cam["device"], self._collect(), cam["name"], self.filter_values
        )
        self.status.set(f"{cam['name']} 저장  {path}")

    def reload_saved(self):
        cam = self.selected
        if cam is None or not cam.get("device"):
            return
        saved = load_camera(cam["id"])
        self.filter_values = load_filters(cam["id"])
        if not saved and not filters_active(self.filter_values):
            self.status.set(f"{cam['name']} 저장값이 없습니다.")
            return
        try:
            if saved:
                set_controls(cam["device"], saved)
            self._rebuild_controls()
            self.status.set(f"{cam['name']} 저장값을 다시 넣었습니다.")
        except Exception as exc:
            self.status.set(f"불러오기 실패: {exc}")

    def reset_defaults(self):
        cam = self.selected
        if cam is None or not cam.get("device"):
            return
        defaults = {item["name"]: item["default"] for item in self.controls}
        self.filter_values = dict(FILTER_DEFAULTS)
        try:
            set_controls(cam["device"], defaults)
            self._rebuild_controls()
            self.status.set(f"{cam['name']} 장치 기본값·필터 끄기로 되돌렸습니다. 저장은 따로 누르세요.")
        except Exception as exc:
            self.status.set(f"기본값 실패: {exc}")

    def light_on(self):
        try:
            value = int(self.light_brightness.get())
        except ValueError:
            self.status.set("조명 밝기는 숫자로 입력하세요.")
            return
        value = max(0, min(80, value))
        self._send_light(f"B:{value}", f"조명 켜짐 B:{value}")

    def light_off(self):
        self._send_light("OFF", "조명 꺼짐")

    def _send_light(self, command, ok_message):
        if self.light_busy:
            return
        self.light_busy = True
        self.status.set(f"조명 보내는 중... {command}")

        def worker():
            try:
                reply = send_ok(command, timeout=3.0, must_reply=True)
                extra = reply or "응답 없음"
                self.root.after(0, lambda: self.status.set(f"{ok_message}   {extra}"))
            except Exception as exc:
                message = str(exc)
                self.root.after(0, lambda m=message: self.status.set(f"조명 실패: {m}"))
            finally:
                self.light_busy = False

        threading.Thread(target=worker, daemon=True).start()

    def _show_image(self, img, score):
        img = img.copy()
        img.thumbnail((1280, 960))
        photo = ImageTk.PhotoImage(img)
        self.photo = photo
        self.preview.config(image=photo, text="")
        self.score_var.set(f"선명도 점수  {score:.0f}   (높을수록 또렷)")

    def _show_text(self, text):
        self.preview.config(image="", text=text)

    def _preview_loop(self):
        while not self.stopped:
            if self.live.last_error:
                err = self.live.last_error
                self.live.last_error = ""
                self.root.after(0, lambda m=err: self._show_text(m))
            try:
                png = self.live.frames.get(timeout=0.2)
            except queue.Empty:
                continue
            if self.stopped:
                break
            try:
                img = Image.open(BytesIO(png)).convert("RGB")
                if self._preview_filters:
                    img = apply_filters(img, dict(self.filter_values))
                score = sharpness_score(img)
            except Exception as exc:
                self.root.after(0, lambda m=str(exc): self._show_text(m))
                continue
            self.root.after(0, lambda i=img, s=score: self._show_image(i, s))

    def _on_close(self):
        self.stopped = True
        self.live.stop()
        try:
            send_ok("OFF", timeout=2.0, must_reply=False)
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    CalibUi().run()
