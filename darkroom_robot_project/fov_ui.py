"""면별 디지털 화각 + 샘플 색 추적 UI.

C270 은 렌즈가 고정이라 광학 줌이 없다. 1280×720 안에서 영역을 잘라 쓴다.
같은 카메라라도 1차·2차는 거치대가 덜 돌아가면 샘플 위치가 달라지므로
면마다 화각을 따로 둔다. 면 3·5는 카메라 3, 면 4·6은 카메라 4.

샘플 색을 찍으면 매 촬영마다 그 색의 바깥 테두리만 따라간다. 노란 화각은
찾는 창이고, 실제 검사는 청록 외곽이다. 스크래치·찌그러짐은 안쪽이라
테두리에서 빼지 않는다.

저장값은 ~/darkroom_calib.json 의 faces.<면키>.fov / sample_color 에 남는다.

  python fov_ui.py

원격 운영 UI·캘리브 창과 동시에 켜지 말 것 — 같은 카메라를 두고 싸운다.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from io import BytesIO
from tkinter import DISABLED, NORMAL, Canvas, Frame, Label

from PIL import Image, ImageDraw, ImageTk

from arduino_link import resolve_port, send_ok
from camera import CAMERAS, LiveCamera
from camera_calib import (
    FOV_DEFAULTS,
    FOV_MIN_PX,
    FOV_SHAPE_LABELS,
    FOV_SHAPES,
    apply_fov,
    apply_saved,
    fov_box,
    fov_from_box,
    load_fov,
    load_sample_color,
    normalize_fov,
    save_fov,
    save_sample_color,
)
from dataset_label import FACES
from light_tone import LIGHT_TONE_K, light_command
from sample_roi import crop_detected, pick_color, tols_from_allowance

SRC_W = 1280
SRC_H = 720
VIEW_W = 960
VIEW_H = 540
RESULT_W = 480
RESULT_H = 270
HANDLE_R = 7
HANDLES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")
CURSORS = {
    "move": "fleur",
    "n": "sb_v_double_arrow",
    "s": "sb_v_double_arrow",
    "e": "sb_h_double_arrow",
    "w": "sb_h_double_arrow",
    "nw": "top_left_corner",
    "se": "bottom_right_corner",
    "ne": "top_right_corner",
    "sw": "bottom_left_corner",
}


class FovUi:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("면별 화각")
        self.root.geometry("1680x960")
        self.root.minsize(1400, 820)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.selected = None
        self.selected_face = None
        self.cam_buttons = {}
        self.face_buttons = {}
        self._servo_stage = None
        self.shape_buttons = {}
        self.fov = dict(FOV_DEFAULTS)
        self.sample_color = None
        self.pick_color_mode = False
        self.tol_var = tk.IntVar(value=50)
        self.color_var = tk.StringVar(value="샘플 색  —  아직 없음")
        self._crop_cache = None
        self._crop_tick = 0
        self.w_var = tk.IntVar(value=100)
        self.h_var = tk.IntVar(value=100)
        self.cx_var = tk.IntVar(value=50)
        self.cy_var = tk.IntVar(value=50)
        self.box_var = tk.StringVar(value="화각  —")
        self.status = tk.StringVar(value="면 1~6 중 하나를 고르세요. 2차 면은 거치대를 돌린 뒤 맞춥니다.")
        self.live = LiveCamera(width=SRC_W, height=SRC_H, fps=12)
        self.photo = None
        self.result_photo = None
        self.frame = None
        self.drag = None
        self.h_scale = None
        self.light_busy = False
        self.stopped = False
        self._syncing = False

        self._build()
        threading.Thread(target=self._preview_loop, daemon=True).start()

    def _build(self):
        top = Frame(self.root)
        top.pack(fill="x", padx=10, pady=(10, 4))
        tk.Button(top, text="이 면 저장", width=14, height=2, bg="#fde68a", command=self.save).pack(
            side="right", padx=4
        )
        tk.Button(top, text="전체 화면", width=12, height=2, command=self.reset_full).pack(side="right", padx=4)
        tk.Label(top, text="면별 화각", font=("Arial", 14, "bold")).pack(side="left")
        tk.Label(
            top,
            text="면 3과 면 5, 면 4와 면 6은 같은 카메라라도 따로 맞춥니다. 샘플을 클릭해 색을 찍으면 위치가 조금 바뀌어도 테두리를 따라갑니다.",
            fg="#555",
        ).pack(side="left", padx=12)

        cams = Frame(self.root)
        cams.pack(fill="x", padx=10, pady=6)
        tk.Label(cams, text="1차", font=("Arial", 10, "bold")).pack(side="left", padx=(0, 8))
        for face in FACES:
            if face["stage"] != 1:
                continue
            self._face_button(cams, face)
        row2 = Frame(self.root)
        row2.pack(fill="x", padx=10, pady=(0, 6))
        tk.Label(row2, text="2차", font=("Arial", 10, "bold")).pack(side="left", padx=(0, 8))
        for face in FACES:
            if face["stage"] != 2:
                continue
            self._face_button(row2, face)

        lights = Frame(self.root, bd=1, relief="groove")
        lights.pack(fill="x", padx=10, pady=(0, 6))
        tk.Label(lights, text="조명", font=("Arial", 10, "bold")).pack(side="left", padx=(8, 6), pady=8)
        tk.Button(lights, text="켜기", width=8, height=1, bg="#fde68a", command=self.light_on).pack(side="left", padx=4)
        tk.Button(lights, text="끄기", width=8, height=1, command=self.light_off).pack(side="left", padx=4)
        tk.Label(
            lights,
            text=f"FTDI {resolve_port()}  ·  NeoPixel D7  ·  {LIGHT_TONE_K}K {light_command()}",
            fg="#555",
        ).pack(side="left", padx=10)

        body = Frame(self.root)
        body.pack(fill="both", expand=True, padx=10, pady=4)

        left = Frame(body, bd=1, relief="solid")
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        tk.Label(left, text="전체 화면  ·  노란 상자 = 찾는 창  ·  청록 = 샘플 테두리", font=("Arial", 10, "bold")).pack(pady=(6, 0))
        self.canvas = Canvas(left, width=VIEW_W, height=VIEW_H, bg="#111", highlightthickness=0, cursor="fleur")
        self.canvas.pack(padx=8, pady=8)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", lambda e: self._nudge_size(1.05))
        self.canvas.bind("<Button-5>", lambda e: self._nudge_size(1 / 1.05))

        right = Frame(body, width=520)
        right.pack(side="left", fill="y")
        right.pack_propagate(False)
        tk.Label(right, text="추적된 샘플", font=("Arial", 10, "bold")).pack(pady=(6, 0))
        self.result = Label(right, text="대기", bg="#222", fg="#ddd", width=60, height=16)
        self.result.pack(padx=8, pady=8)

        ctrl = Frame(right, bd=1, relief="groove")
        ctrl.pack(fill="x", padx=8, pady=(4, 8))
        tk.Label(ctrl, text="모양", font=("Arial", 10, "bold")).pack(anchor="w", padx=8, pady=(8, 2))
        shapes = Frame(ctrl)
        shapes.pack(fill="x", padx=8, pady=(0, 8))
        for key in FOV_SHAPES:
            btn = tk.Button(
                shapes,
                text=FOV_SHAPE_LABELS[key],
                width=10,
                height=2,
                command=lambda s=key: self.set_shape(s),
            )
            btn.pack(side="left", padx=3)
            self.shape_buttons[key] = btn
        tk.Label(
            ctrl,
            text="모서리·변을 잡아 모양을 바꿉니다. 안쪽을 드래그하면 이동합니다. 휠은 크기입니다.",
            fg="#666",
            wraplength=460,
            justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 6))
        self._slider(ctrl, "가로 크기", self.w_var, 8, 100, 1, self._on_size, "%")
        self.h_scale = self._slider(ctrl, "세로 크기", self.h_var, 8, 100, 1, self._on_size, "%")
        self._slider(ctrl, "가로 위치", self.cx_var, 0, 100, 1, self._on_pan, "%")
        self._slider(ctrl, "세로 위치", self.cy_var, 0, 100, 1, self._on_pan, "%")
        tk.Label(ctrl, textvariable=self.box_var, fg="#333").pack(anchor="w", padx=8, pady=(4, 6))

        color_box = Frame(ctrl, bd=1, relief="groove")
        color_box.pack(fill="x", padx=8, pady=(0, 10))
        tk.Label(color_box, text="샘플 색 추적", font=("Arial", 10, "bold")).pack(anchor="w", padx=8, pady=(8, 2))
        tk.Label(
            color_box,
            text="샘플 안쪽(흠집·찌그러짐이 있는 면)을 클릭하세요. 테두리는 바깥만 잡고, 안쪽 구멍은 메웁니다.",
            fg="#555",
            wraplength=460,
            justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 6))
        color_row = Frame(color_box)
        color_row.pack(fill="x", padx=8, pady=(0, 6))
        self.pick_btn = tk.Button(
            color_row,
            text="샘플 색 찍기",
            width=14,
            height=2,
            command=self._toggle_pick,
        )
        self.pick_btn.pack(side="left")
        self.swatch = tk.Label(color_row, width=4, height=2, bg="#333", relief="solid", bd=1)
        self.swatch.pack(side="left", padx=8)
        tk.Label(color_row, textvariable=self.color_var, wraplength=280, justify="left").pack(
            side="left", fill="x", expand=True
        )
        self._slider(color_box, "색 허용 범위", self.tol_var, 15, 80, 1, self._on_tol, "")
        self._refresh_shape_buttons()

        status_row = Frame(self.root)
        status_row.pack(fill="x", padx=10, pady=6)
        tk.Label(status_row, textvariable=self.status, wraplength=1500, justify="left").pack(side="left", padx=4)

    def _cam_by_id(self, cam_id):
        for cam in CAMERAS:
            if cam["id"] == int(cam_id):
                return cam
        return {"id": int(cam_id), "name": f"카메라 {cam_id}", "device": None}

    def _face_button(self, parent, face):
        cam = self._cam_by_id(face["cam"])
        connected = bool(cam.get("device"))
        stage = "1차" if face["stage"] == 1 else "2차"
        text = f"{face['name']}  ·  카메라 {face['cam']}  ·  {stage}"
        btn = tk.Button(
            parent,
            text=text,
            width=22,
            height=2,
            state=NORMAL if connected else DISABLED,
            command=lambda f=face: self.select_face(f),
        )
        btn.pack(side="left", padx=4)
        self.face_buttons[face["key"]] = btn
        return btn

    def select_face(self, face):
        cam = self._cam_by_id(face["cam"])
        self.selected_face = face
        self.selected = cam
        for key, btn in self.face_buttons.items():
            if key == face["key"]:
                btn.config(relief="sunken", bg="#fde68a")
            else:
                btn.config(relief="raised", bg=self.root.cget("bg"))
        if not cam.get("device"):
            self.live.stop()
            self.status.set(f"{face['name']} 카메라가 아직 없습니다.")
            self._clear_views("미연결")
            return
        self._load_fov(cam["id"], face["stage"])
        self._load_color(cam["id"], face["stage"])
        try:
            apply_saved(cam["id"], cam["device"])
        except Exception:
            pass
        self.live.start(cam["device"])
        self._set_mount(face["stage"])
        self.status.set(self._face_status())

    def select(self, cam):
        """예전 카메라 선택. 1차 면으로 연결."""
        for face in FACES:
            if face["cam"] == cam["id"] and face["stage"] == 1:
                self.select_face(face)
                return
        self.selected_face = None
        self.selected = cam
        self._load_fov(cam["id"], 1)
        if cam.get("device"):
            self.live.start(cam["device"])

    def _face_status(self):
        face = self.selected_face or {}
        cam = self.selected or {}
        stage = "1차" if face.get("stage") == 1 else "2차"
        name = FOV_SHAPE_LABELS.get(self.fov.get("shape"), self.fov.get("shape"))
        return (
            f"{face.get('name', cam.get('name', ''))}  {cam.get('device', '')}  ·  "
            f"{stage}  ·  {name}  ·  "
            + ("색 추적 켜짐" if self.sample_color else "색 없음 · 고정 화각")
            + "  ·  저장을 눌러야 이 면 촬영에 반영됩니다"
        )

    def _set_mount(self, stage):
        stage = 2 if int(stage) == 2 else 1
        if self._servo_stage == stage:
            return
        self.status.set("거치대 회전 중...")

        def worker():
            try:
                from servo import home, rotate_180

                if stage == 2:
                    rotate_180()
                    msg = "2차 위치"
                else:
                    home()
                    msg = "1차 위치"
                self._servo_stage = stage
                self.root.after(0, lambda: self.status.set(f"{self._face_status()}  ·  {msg}"))
            except Exception as exc:
                message = str(exc)
                self.root.after(0, lambda m=message: self.status.set(f"서보 실패: {m}"))

        threading.Thread(target=worker, daemon=True).start()

    def _slider(self, parent, label, var, lo, hi, step, command, suffix):
        row = Frame(parent)
        row.pack(fill="x", padx=8, pady=4)
        tk.Label(row, text=label, width=12, anchor="w").pack(side="left")
        shown = tk.Label(row, width=6, anchor="e")
        shown.pack(side="right")

        def _show(val, widget=shown, unit=suffix):
            widget.config(text=f"{int(float(val))}{unit}")

        _show(var.get())
        scale = tk.Scale(
            row,
            from_=lo,
            to=hi,
            resolution=step,
            orient="horizontal",
            showvalue=0,
            length=280,
            variable=var,
            command=lambda val, fn=command, sh=_show: (sh(val), fn()),
        )
        scale.pack(side="left", fill="x", expand=True, padx=6)
        return scale

    def set_shape(self, shape):
        box = fov_box(SRC_W, SRC_H, self.fov)
        self.fov = fov_from_box(SRC_W, SRC_H, box, shape)
        self._push_sliders()
        self._refresh_shape_buttons()
        self._refresh_box_label()
        self._redraw()
        self.status.set(f"모양  {FOV_SHAPE_LABELS.get(shape, shape)}  ·  저장을 눌러야 촬영에 반영됩니다.")

    def _refresh_shape_buttons(self):
        current = self.fov.get("shape", "rect")
        for key, btn in self.shape_buttons.items():
            if key == current:
                btn.config(relief="sunken", bg="#fde68a")
            else:
                btn.config(relief="raised", bg=self.root.cget("bg"))
        if self.h_scale is not None:
            self.h_scale.config(state=DISABLED if current == "circle" else NORMAL)

    def _load_fov(self, cam_id, stage=1):
        self.fov = load_fov(cam_id, stage=stage)
        self._push_sliders()
        self._refresh_shape_buttons()
        self._refresh_box_label()

    def _load_color(self, cam_id, stage=1):
        self.sample_color = load_sample_color(cam_id, stage=stage)
        self.pick_color_mode = False
        self._crop_cache = None
        if self.sample_color:
            self._syncing = True
            self.tol_var.set(int(self.sample_color.get("s_tol") or 50))
            self._syncing = False
        self._refresh_color_ui()

    def _toggle_pick(self):
        if self.frame is None or self.selected is None:
            self.status.set("면을 고른 뒤 미리보기가 뜨면 샘플을 클릭하세요.")
            return
        self.pick_color_mode = not self.pick_color_mode
        self._refresh_color_ui()
        if self.pick_color_mode:
            self.canvas.config(cursor="crosshair")
            self.status.set("샘플 안쪽을 클릭하세요. 흠집·찌그러짐이 있는 면도 됩니다 — 바깥 테두리만 잡습니다.")
        else:
            self.canvas.config(cursor="fleur")
            self.status.set(self._face_status())

    def _hsv_to_hex(self, color):
        from PIL import Image as _Image

        img = _Image.new("HSV", (1, 1), (int(color["h"]), int(color["s"]), int(color["v"])))
        r, g, b = img.convert("RGB").getpixel((0, 0))
        return f"#{r:02x}{g:02x}{b:02x}"

    def _refresh_color_ui(self):
        if self.sample_color:
            hex_color = self._hsv_to_hex(self.sample_color)
            self.swatch.config(bg=hex_color)
            self.color_var.set(
                f"H{self.sample_color['h']} S{self.sample_color['s']} V{self.sample_color['v']}  "
                f"허용 {self.tol_var.get()}"
            )
        else:
            self.swatch.config(bg="#333")
            self.color_var.set("샘플 색  —  아직 없음")
        if getattr(self, "pick_btn", None) is not None:
            if self.pick_color_mode:
                self.pick_btn.config(relief="sunken", bg="#67e8f9")
            else:
                self.pick_btn.config(relief="raised", bg=self.root.cget("bg"))

    def _on_tol(self):
        if self._syncing or not self.sample_color:
            return
        self.sample_color.update(tols_from_allowance(self.tol_var.get(), self.sample_color))
        self._crop_cache = None
        self._refresh_color_ui()
        self._redraw()

    def _apply_picked_color(self, color):
        if not color:
            self.status.set("그 지점에서 색을 읽지 못했습니다. 샘플 안쪽을 다시 클릭하세요.")
            return
        color.update(tols_from_allowance(self.tol_var.get(), color))
        self.sample_color = color
        self.pick_color_mode = False
        self._crop_cache = None
        self.canvas.config(cursor="fleur")
        self._refresh_color_ui()
        self._redraw()
        self.status.set("샘플 색을 잡았습니다. 청록 테두리를 확인하고 저장을 누르세요.")

    def _push_sliders(self):
        self._syncing = True
        self.w_var.set(int(round(self.fov["w"] * 100)))
        self.h_var.set(int(round(self.fov["h"] * 100)))
        self.cx_var.set(int(round((self.fov["x"] + self.fov["w"] / 2.0) * 100)))
        self.cy_var.set(int(round((self.fov["y"] + self.fov["h"] / 2.0) * 100)))
        self._syncing = False

    def _read_sliders(self):
        w = max(0.08, self.w_var.get() / 100.0)
        h = max(0.08, self.h_var.get() / 100.0)
        cx = self.cx_var.get() / 100.0
        cy = self.cy_var.get() / 100.0
        self.fov = normalize_fov(
            {
                "shape": self.fov.get("shape", "rect"),
                "x": cx - w / 2.0,
                "y": cy - h / 2.0,
                "w": w,
                "h": h,
            },
            SRC_W,
            SRC_H,
        )

    def _on_size(self):
        if self._syncing:
            return
        if self.fov.get("shape") == "circle":
            self._syncing = True
            self.h_var.set(self.w_var.get())
            self._syncing = False
        self._read_sliders()
        self._push_sliders()
        self._refresh_box_label()
        self._redraw()

    def _on_pan(self):
        if self._syncing:
            return
        self._read_sliders()
        self._push_sliders()
        self._refresh_box_label()
        self._redraw()

    def _nudge_size(self, factor):
        if self.selected is None:
            return
        cx = self.fov["x"] + self.fov["w"] / 2.0
        cy = self.fov["y"] + self.fov["h"] / 2.0
        w = min(1.0, max(FOV_MIN_PX / SRC_W, self.fov["w"] * factor))
        h = min(1.0, max(FOV_MIN_PX / SRC_H, self.fov["h"] * factor))
        self.fov = normalize_fov(
            {"shape": self.fov.get("shape", "rect"), "x": cx - w / 2.0, "y": cy - h / 2.0, "w": w, "h": h},
            SRC_W,
            SRC_H,
        )
        self._push_sliders()
        self._refresh_box_label()
        self._redraw()

    def _on_wheel(self, event):
        self._nudge_size(1.05 if event.delta > 0 else 1 / 1.05)

    def _refresh_box_label(self):
        x0, y0, x1, y1 = fov_box(SRC_W, SRC_H, self.fov)
        w, h = x1 - x0 + 1, y1 - y0 + 1
        name = FOV_SHAPE_LABELS.get(self.fov.get("shape"), "직사각형")
        self.box_var.set(f"{name}  {w}×{h}  @ ({x0},{y0})  ·  원본 {SRC_W}×{SRC_H}")

    def _view_rect(self):
        x0, y0, x1, y1 = fov_box(SRC_W, SRC_H, self.fov)
        sx, sy = VIEW_W / SRC_W, VIEW_H / SRC_H
        return x0 * sx, y0 * sy, (x1 + 1) * sx, (y1 + 1) * sy

    def _handle_points(self, rx0, ry0, rx1, ry1):
        mx, my = (rx0 + rx1) / 2.0, (ry0 + ry1) / 2.0
        return {
            "nw": (rx0, ry0),
            "n": (mx, ry0),
            "ne": (rx1, ry0),
            "e": (rx1, my),
            "se": (rx1, ry1),
            "s": (mx, ry1),
            "sw": (rx0, ry1),
            "w": (rx0, my),
        }

    def _hit(self, x, y):
        rx0, ry0, rx1, ry1 = self._view_rect()
        for name, (hx, hy) in self._handle_points(rx0, ry0, rx1, ry1).items():
            if abs(x - hx) <= HANDLE_R + 2 and abs(y - hy) <= HANDLE_R + 2:
                return name
        if rx0 <= x <= rx1 and ry0 <= y <= ry1:
            return "move"
        return None

    def _on_hover(self, event):
        if self.drag:
            return
        if self.pick_color_mode:
            try:
                self.canvas.config(cursor="crosshair")
            except tk.TclError:
                pass
            return
        hit = self._hit(event.x, event.y)
        cursor = CURSORS.get(hit, "arrow")
        try:
            self.canvas.config(cursor=cursor)
        except tk.TclError:
            self.canvas.config(cursor="arrow")

    def _on_press(self, event):
        if self.frame is None:
            return
        if self.pick_color_mode:
            sx, sy = SRC_W / VIEW_W, SRC_H / VIEW_H
            color = pick_color(self.frame, int(event.x * sx), int(event.y * sy))
            self._apply_picked_color(color)
            return
        hit = self._hit(event.x, event.y) or "move"
        x0, y0, x1, y1 = fov_box(SRC_W, SRC_H, self.fov)
        self.drag = {
            "mode": hit,
            "x": event.x,
            "y": event.y,
            "box": (x0, y0, x1, y1),
        }

    def _on_drag(self, event):
        if not self.drag:
            return
        sx, sy = SRC_W / VIEW_W, SRC_H / VIEW_H
        dx = int(round((event.x - self.drag["x"]) * sx))
        dy = int(round((event.y - self.drag["y"]) * sy))
        x0, y0, x1, y1 = self.drag["box"]
        mode = self.drag["mode"]
        if mode == "move":
            w, h = x1 - x0, y1 - y0
            x0 = max(0, min(SRC_W - 1 - w, x0 + dx))
            y0 = max(0, min(SRC_H - 1 - h, y0 + dy))
            x1, y1 = x0 + w, y0 + h
        else:
            x0, y0, x1, y1 = self._resize_box(x0, y0, x1, y1, mode, dx, dy)
        self.fov = fov_from_box(SRC_W, SRC_H, (x0, y0, x1, y1), self.fov.get("shape", "rect"))
        self._push_sliders()
        self._refresh_box_label()
        self._redraw()

    def _resize_box(self, x0, y0, x1, y1, mode, dx, dy):
        nx0, ny0, nx1, ny1 = x0, y0, x1, y1
        if "n" in mode:
            ny0 = y0 + dy
        if "s" in mode:
            ny1 = y1 + dy
        if "w" in mode:
            nx0 = x0 + dx
        if "e" in mode:
            nx1 = x1 + dx
        if self.fov.get("shape") == "circle":
            if mode in ("n", "s"):
                cy = (y0 + y1) / 2.0
                ch = max(FOV_MIN_PX, abs(ny1 - ny0) + 1)
                nx0 = int(round((x0 + x1 + 1) / 2.0 - ch / 2.0))
                nx1 = nx0 + int(ch) - 1
                ny0 = int(round(cy - ch / 2.0))
                ny1 = ny0 + int(ch) - 1
            elif mode in ("e", "w"):
                cx = (x0 + x1) / 2.0
                cw = max(FOV_MIN_PX, abs(nx1 - nx0) + 1)
                ny0 = int(round((y0 + y1 + 1) / 2.0 - cw / 2.0))
                ny1 = ny0 + int(cw) - 1
                nx0 = int(round(cx - cw / 2.0))
                nx1 = nx0 + int(cw) - 1
            else:
                side = max(FOV_MIN_PX, max(abs(nx1 - nx0), abs(ny1 - ny0)) + 1)
                if "w" in mode:
                    nx0 = x1 - side + 1
                    nx1 = x1
                else:
                    nx0 = x0
                    nx1 = x0 + side - 1
                if "n" in mode:
                    ny0 = y1 - side + 1
                    ny1 = y1
                else:
                    ny0 = y0
                    ny1 = y0 + side - 1
        if nx1 < nx0:
            nx0, nx1 = nx1, nx0
        if ny1 < ny0:
            ny0, ny1 = ny1, ny0
        if nx1 - nx0 + 1 < FOV_MIN_PX:
            if "w" in mode:
                nx0 = nx1 - FOV_MIN_PX + 1
            else:
                nx1 = nx0 + FOV_MIN_PX - 1
        if ny1 - ny0 + 1 < FOV_MIN_PX:
            if "n" in mode:
                ny0 = ny1 - FOV_MIN_PX + 1
            else:
                ny1 = ny0 + FOV_MIN_PX - 1
        nx0 = max(0, min(SRC_W - FOV_MIN_PX, nx0))
        ny0 = max(0, min(SRC_H - FOV_MIN_PX, ny0))
        nx1 = max(nx0 + FOV_MIN_PX - 1, min(SRC_W - 1, nx1))
        ny1 = max(ny0 + FOV_MIN_PX - 1, min(SRC_H - 1, ny1))
        return nx0, ny0, nx1, ny1

    def _on_release(self, _event):
        self.drag = None

    def save(self):
        cam = self.selected
        face = self.selected_face
        if cam is None or not cam.get("device"):
            self.status.set("저장할 면이 없습니다.")
            return
        self._read_sliders()
        stage = int((face or {}).get("stage") or 1)
        path = save_fov(cam["id"], self.fov, cam["device"], cam["name"], stage=stage)
        save_sample_color(cam["id"], self.sample_color, stage=stage)
        name = FOV_SHAPE_LABELS.get(self.fov["shape"], self.fov["shape"])
        label = (face or {}).get("name") or cam["name"]
        extra = "  ·  색 추적" if self.sample_color else "  ·  색 없음"
        self.status.set(f"{label} 저장  {name}{extra}  {path}")

    def reset_full(self):
        self.fov = dict(FOV_DEFAULTS)
        self._push_sliders()
        self._refresh_shape_buttons()
        self._refresh_box_label()
        self._redraw()
        self.status.set("전체 화면·직사각형으로 되돌렸습니다. 저장을 눌러야 촬영에 반영됩니다.")

    def light_on(self):
        self._send_light(light_command(), f"조명 켜짐 {LIGHT_TONE_K}K")

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

    def _clear_views(self, text):
        self.frame = None
        self.canvas.delete("all")
        self.canvas.create_text(VIEW_W // 2, VIEW_H // 2, text=text, fill="#ddd", font=("Arial", 14))
        self.result.config(image="", text=text)

    def _overlay(self, view, rx0, ry0, rx1, ry1, shape):
        base = view.convert("RGBA")
        dimmed = Image.blend(base, Image.new("RGBA", view.size, (0, 0, 0, 255)), 0.45)
        mask = Image.new("L", view.size, 0)
        draw = ImageDraw.Draw(mask)
        box = (int(rx0), int(ry0), int(rx1) - 1, int(ry1) - 1)
        if shape == "rect":
            draw.rectangle(box, fill=255)
        else:
            draw.ellipse(box, fill=255)
        return Image.composite(base, dimmed, mask).convert("RGB")

    def _redraw(self):
        if self.frame is None:
            return
        view = self.frame.resize((VIEW_W, VIEW_H), Image.BILINEAR)
        rx0, ry0, rx1, ry1 = self._view_rect()
        view = self._overlay(view, rx0, ry0, rx1, ry1, self.fov.get("shape", "rect"))
        self.photo = ImageTk.PhotoImage(view)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        shape = self.fov.get("shape", "rect")
        outline = (rx0, ry0, rx1, ry1)
        if shape == "rect":
            self.canvas.create_rectangle(*outline, outline="#111", width=5)
            self.canvas.create_rectangle(*outline, outline="#facc15", width=3)
        else:
            self.canvas.create_oval(*outline, outline="#111", width=5)
            self.canvas.create_oval(*outline, outline="#facc15", width=3)
            self.canvas.create_rectangle(*outline, outline="#facc15", dash=(4, 3), width=1)
        for hx, hy in self._handle_points(rx0, ry0, rx1, ry1).values():
            self.canvas.create_rectangle(
                hx - HANDLE_R,
                hy - HANDLE_R,
                hx + HANDLE_R,
                hy + HANDLE_R,
                fill="#facc15",
                outline="#111",
                width=1,
            )
        label = FOV_SHAPE_LABELS.get(shape, "화각")
        self.canvas.create_text(
            rx0 + 10,
            max(16, ry0 - 12),
            text=label,
            fill="#facc15",
            anchor="w",
            font=("Arial", 11, "bold"),
        )
        crop = None
        if self.sample_color:
            self._crop_tick += 1
            if self._crop_cache is None or self._crop_tick % 4 == 1:
                try:
                    search = fov_box(SRC_W, SRC_H, self.fov)
                    self._crop_cache = crop_detected(self.frame, color=self.sample_color, search_box=search)
                except Exception:
                    self._crop_cache = None
            crop = self._crop_cache
            if crop is not None:
                if crop.corners and len(crop.corners) == 4:
                    sx, sy = VIEW_W / SRC_W, VIEW_H / SRC_H
                    pts = [c for p in crop.corners for c in (p[0] * sx, p[1] * sy)]
                    self.canvas.create_polygon(*pts, outline="#111", width=5, fill="")
                    self.canvas.create_polygon(*pts, outline="#22d3ee", width=3, fill="")
                else:
                    bx0, by0, bx1, by1 = crop.box
                    sx, sy = VIEW_W / SRC_W, VIEW_H / SRC_H
                    box = (bx0 * sx, by0 * sy, (bx1 + 1) * sx, (by1 + 1) * sy)
                    self.canvas.create_rectangle(*box, outline="#111", width=5)
                    self.canvas.create_rectangle(*box, outline="#22d3ee", width=3)
                self.canvas.create_text(
                    12,
                    18,
                    text="샘플 테두리 (안쪽 흠집 포함)",
                    fill="#22d3ee",
                    anchor="w",
                    font=("Arial", 11, "bold"),
                )
        preview = crop.image if crop is not None else apply_fov(self.frame, self.fov)
        preview = preview.resize((RESULT_W, RESULT_H), Image.BILINEAR)
        self.result_photo = ImageTk.PhotoImage(preview)
        self.result.config(image=self.result_photo, text="")

    def _preview_loop(self):
        while not self.stopped:
            if self.live.last_error:
                err = self.live.last_error
                self.live.last_error = ""
                self.root.after(0, lambda m=err: self._clear_views(m))
            try:
                png = self.live.frames.get(timeout=0.2)
            except queue.Empty:
                continue
            if self.stopped:
                break
            try:
                img = Image.open(BytesIO(png)).convert("RGB")
            except Exception as exc:
                self.root.after(0, lambda m=str(exc): self._clear_views(m))
                continue
            self.frame = img
            self.root.after(0, self._redraw)

    def _on_close(self):
        self.stopped = True
        self.live.stop()
        try:
            send_ok("OFF", timeout=2.0, must_reply=False)
        except Exception:
            pass
        if self._servo_stage == 2:
            try:
                from servo import home

                home()
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    FovUi().run()
