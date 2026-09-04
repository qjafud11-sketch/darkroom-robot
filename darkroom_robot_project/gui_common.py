"""gui_server · remote_test_ui 공통 위젯·테마."""
from __future__ import annotations

import json
import socket
import threading
import time
from collections import Counter
from datetime import datetime
from io import BytesIO
from pathlib import Path
import tkinter as tk
from tkinter import DISABLED, END, NORMAL, Button, Canvas, Entry, Frame, Label, Listbox, Scrollbar, StringVar, ttk
from tkinter import font as tkfont

from PIL import Image, ImageDraw, ImageTk

from camera import CAMERAS, CAPTURE_DIR, connected_cameras
from judgment import RESULT_PATH, image_path_from_cam, load_manifest
from ui_store import (
    RECORDS_ROOT,
    clear_history,
    format_judged_at,
    history_summary,
    list_records,
    load_settings,
    save_settings,
    settings_env_snippet,
)

CELL_SIZE = (340, 190)

# 검사 면 구성 — 촬영은 1차 4장 + 2차 2장. 화면은 면 1~6을 3×2로 보여 준다.
INSPECT_FACES = {
    "1차": (
        {"id": 1, "name": "면 1"},
        {"id": 2, "name": "면 2"},
        {"id": 3, "name": "면 3"},
        {"id": 4, "name": "면 4"},
    ),
    "2차": (
        {"id": 3, "name": "면 5"},
        {"id": 4, "name": "면 6"},
    ),
}
INSPECT_FACE_TOTAL = sum(len(v) for v in INSPECT_FACES.values())
INSPECT_GRID = tuple(
    {"inspect": inspect, "id": face["id"], "name": face["name"]}
    for inspect, faces in INSPECT_FACES.items()
    for face in faces
)
INSPECT_GRID_COLS = 3


def face_label(inspect: str, face_id: int) -> str:
    for face in INSPECT_FACES.get(inspect, ()):
        if int(face["id"]) == int(face_id):
            return face["name"]
    return f"면 {face_id}"


CLASS_LABELS = {
    "unknown": "이상",
    "scratch": "스크래치",
    "contamination": "이물",
    "dent": "찌그러짐",
    "edge_break": "모서리 깨짐",
    "dimension": "치수 불량",
    "code_fail": "코드 인식 실패",
    "mock_defect": "테스트 불량",
}


def class_label(class_name: str) -> str:
    key = (class_name or "").strip().lower()
    return CLASS_LABELS.get(key, class_name or "기타")


def defect_line(item: dict) -> str:
    inspect = item.get("inspect", "")
    face_id = int(item.get("cam_id", 0))
    name = class_label(str(item.get("class_name") or "unknown"))
    score = float(item.get("score", 0) or 0)
    threshold = float(item.get("threshold", 0) or 0)
    ng = item.get("ng")
    source = str(item.get("source") or "")
    if source == "yolo" and name == "이상":
        name = "YOLO"
    if threshold > 0:
        mark = "NG" if ng or (ng is None and score >= threshold) else "OK"
        if 0 <= score <= 1.5 and 0 < threshold <= 1.5:
            return (
                f"  {face_label(inspect, face_id)}  {inspect}  "
                f"{mark}  {name}  {score * 100:.0f}%"
            )
        return (
            f"  {face_label(inspect, face_id)}  {inspect}  "
            f"{mark}  {name}  {score:.1f}/{threshold:.1f}"
        )
    percent = score * 100 if score <= 1.5 else score
    return (
        f"  {face_label(inspect, face_id)}  {inspect}  "
        f"{name}  {percent:.0f}%"
    )


LIVE_BACKENDS = ("unsup", "model", "both", "yolo", "unsup+yolo")
YOLO_CLASSES = ("scratch", "dent")


def split_judge_items(judgment: dict | None) -> tuple[list, list]:
    """비지도 면 점수와 YOLO 검출을 나눈다."""
    payload = judgment or {}
    scores = list(payload.get("scores") or [])
    defects = list(payload.get("defects") or [])
    if scores:
        unsup = [row for row in scores if row.get("source") == "unsup"]
        yolo = [
            row for row in scores
            if row.get("source") == "yolo"
            and str(row.get("class_name") or "").strip().lower() in YOLO_CLASSES
        ]
        if not unsup:
            unsup = [row for row in scores if row.get("source") not in ("unsup", "yolo")]
        return unsup, yolo
    unsup, yolo = [], []
    for item in defects:
        key = str(item.get("class_name") or "unknown").strip().lower()
        if key in YOLO_CLASSES:
            yolo.append(item)
        else:
            unsup.append(item)
    return unsup, yolo


def yolo_category_summary(items: list) -> str:
    counts = Counter()
    for item in items:
        key = str(item.get("class_name") or "").strip().lower()
        if key in YOLO_CLASSES:
            counts[key] += 1
        elif item.get("ng"):
            counts[key or "unknown"] += 1
    if not counts:
        return "검출 없음"
    parts = []
    for key in YOLO_CLASSES:
        if counts.get(key):
            parts.append(f"{CLASS_LABELS.get(key, key)} {counts[key]}건")
    for key, n in counts.items():
        if key not in YOLO_CLASSES:
            parts.append(f"{class_label(key)} {n}건")
    return " · ".join(parts)


def _bbox_key(bbox):
    if not bbox or len(bbox) < 4:
        return None
    return tuple(int(round(float(x))) for x in bbox[:4])

PHASE_LABELS = {
    "PICK": "샘플 집는 중",
    "INSERT": "투입 중",
    "INSPECT_1": "1차 검사중",
    "GAP": "",
    "FLIP": "뒤집는 중",
    "INSPECT_2": "2차 검사중",
    "BRINGOUT": "판정중",
    "JUDGE": "판정중",
    "SORT": "분류 중",
    "REPORT": "완료",
    "PING": "통신 확인",
}

TIMELINE_STEPS = (
    ("0", "PICK", "집기"),
    ("1", "INSERT", "투입"),
    ("2", "INSPECT_1", "1차"),
    ("4", "FLIP", "뒤집기"),
    ("5", "INSPECT_2", "2차"),
    ("7+8", "BRINGOUT", "판정"),
    ("9", "SORT", "분류"),
    ("10", "REPORT", "완료"),
)

# Vision Mate / MES / control-room inspired palette — One UI 계열 다크 서피스
COLORS = {
    "bg_root": "#080D1A",
    "bg_sidebar": "#0B111F",
    "bg_card": "#151E30",
    "bg_card_alt": "#1C2740",
    "bg_cell": "#0F1626",
    "bg_glass": "#182136",
    "border": "#2A3752",
    "border_soft": "#1F2A42",
    "divider": "#212C44",
    "scrollbar": "#2A3752",
    "scrollbar_hover": "#22D3EE",
    "border_active": "#22D3EE",
    "text_primary": "#F8FAFC",
    "text_secondary": "#9CA9BC",
    "text_dim": "#6B7A92",
    "accent": "#22D3EE",
    "accent_hover": "#06B6D4",
    "accent_soft": "#164E63",
    "ok": "#4ADE80",
    "ok_bg": "#14532D",
    "ok_glow": "#166534",
    "ng": "#FB7185",
    "ng_bg": "#881337",
    "warn": "#FBBF24",
    "run_bg": "#1E3A5F",
    "stop_bg": "#374151",
    "stop_active": "#BE123C",
    # 검사 정지 — 대기(어두운 주황빨강) / 활성(선명한 빨강)
    "stop_off_bg": "#7F1D1D",
    "stop_off_fg": "#FECACA",
    "stop_on_bg": "#EF4444",
    "stop_on_hover": "#F97316",
    # legacy aliases
    "bg_dark": "#080D1A",
    "bg_panel": "#151E30",
    "text_muted": "#9CA9BC",
    "text_light": "#F1F5F9",
    "ok_fg": "#BBF7D0",
    "ng_fg": "#FECACA",
    "run_fg": "#93C5FD",
    "idle_bg": "#1A2438",
    "accent_stop": "#B91C1C",
}

FONT_TITLE = ("Segoe UI", 16, "bold")
FONT_HEAD = ("Segoe UI", 12, "bold")
FONT_BODY = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 9)
FONT_HERO = ("Segoe UI", 42, "bold")
FONT_PHASE = ("Segoe UI", 18, "bold")
FONT_STAT = ("Segoe UI", 22, "bold")

_THEME_READY = False


def ensure_ui_theme(root: tk.Misc | None = None) -> ttk.Style:
    """ttk 스크롤바 등 공통 위젯 테마."""
    global _THEME_READY
    style = ttk.Style(root)
    if _THEME_READY:
        return style
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(
        "Dark.Vertical.TScrollbar",
        background=COLORS["scrollbar"],
        troughcolor=COLORS["bg_root"],
        bordercolor=COLORS["bg_root"],
        lightcolor=COLORS["bg_root"],
        darkcolor=COLORS["bg_root"],
        relief="flat",
        borderwidth=0,
        arrowsize=0,
        width=6,
    )
    style.map(
        "Dark.Vertical.TScrollbar",
        background=[
            ("active", COLORS["scrollbar_hover"]),
            ("pressed", COLORS["accent"]),
            ("!active", COLORS["scrollbar"]),
        ],
    )
    _THEME_READY = True
    return style


class ScrollableFrame(Frame):
    """세로 스크롤 — 얇은 다크 스크롤바 + 마우스 휠."""

    def __init__(self, parent, bg=None, *, pad_right=0):
        bg = bg or COLORS["bg_card"]
        super().__init__(parent, bg=bg)
        ensure_ui_theme(parent.winfo_toplevel())
        self._canvas = Canvas(self, bg=bg, highlightthickness=0, borderwidth=0)
        self._scroll = ttk.Scrollbar(
            self, orient="vertical", command=self._canvas.yview, style="Dark.Vertical.TScrollbar",
        )
        self.content = Frame(self._canvas, bg=bg)
        self._win = self._canvas.create_window((0, 0), window=self.content, anchor="nw")
        self._canvas.configure(yscrollcommand=self._scroll.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._scroll.pack(side="right", fill="y", padx=(4, pad_right))
        self.content.bind("<Configure>", self._on_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        for widget in (self, self._canvas, self.content):
            widget.bind("<Enter>", self._bind_wheel)
            widget.bind("<Leave>", self._unbind_wheel)

    def _on_configure(self, _event=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self._canvas.itemconfigure(self._win, width=event.width)

    def _bind_wheel(self, _event=None):
        self._canvas.bind_all("<MouseWheel>", self._on_wheel)
        self._canvas.bind_all("<Button-4>", self._on_wheel_linux)
        self._canvas.bind_all("<Button-5>", self._on_wheel_linux)

    def _unbind_wheel(self, _event=None):
        self._canvas.unbind_all("<MouseWheel>")
        self._canvas.unbind_all("<Button-4>")
        self._canvas.unbind_all("<Button-5>")

    def _on_wheel(self, event):
        self._canvas.yview_scroll(int(-event.delta / 120), "units")

    def _on_wheel_linux(self, event):
        self._canvas.yview_scroll(-1 if event.num == 4 else 1, "units")

    def scroll_to_top(self):
        self._canvas.yview_moveto(0)


def _section_label(parent, text: str, *, subtitle: str = ""):
    bg = COLORS["bg_card"]
    wrap = Frame(parent, bg=bg)
    wrap.pack(fill="x", pady=(12, 6))
    head = Frame(wrap, bg=bg)
    head.pack(fill="x")
    accent = Frame(head, bg=COLORS["accent"], width=3, height=14)
    accent.pack(side="left", padx=(0, 8))
    accent.pack_propagate(False)
    Label(head, text=text, font=FONT_HEAD, fg=COLORS["text_primary"], bg=bg).pack(side="left")
    if subtitle:
        Label(
            wrap, text=subtitle, font=FONT_SMALL, fg=COLORS["text_dim"],
            bg=bg, anchor="w",
        ).pack(fill="x", padx=(11, 0), pady=(2, 0))
    Frame(wrap, bg=COLORS["divider"], height=1).pack(fill="x", pady=(10, 0))
    return wrap


def phase_label(command: str) -> str:
    return PHASE_LABELS.get(command, command)


def _overlay_bboxes(img, bboxes, highlight_idx=-1, orig_size=None):
    """화면 크기에 맞춘 뒤에 빨간 박스만 그린다."""
    if not bboxes:
        return
    orig_w, orig_h = orig_size or img.size
    w, h = img.size
    if orig_w <= 0 or orig_h <= 0:
        return
    sx, sy = w / orig_w, h / orig_h
    draw = ImageDraw.Draw(img)
    for idx, bbox in enumerate(bboxes):
        if not bbox or len(bbox) < 4:
            continue
        x1, y1, x2, y2 = bbox[:4]
        box = [x1 * sx, y1 * sy, x2 * sx, y2 * sy]
        width = 4 if idx == highlight_idx else 3
        draw.rectangle(box, outline="#EF4444", width=width)


def render_image(path, bboxes=None, highlight_idx=-1, size=CELL_SIZE, cam_id=None, inspect=None):
    if cam_id is not None:
        from camera_calib import inspect_stage, open_camera_rgb

        img = open_camera_rgb(path, cam_id, stage=inspect_stage(inspect))
    else:
        img = Image.open(path).convert("RGB")
    orig_size = img.size
    img.thumbnail(size)
    _overlay_bboxes(img, bboxes, highlight_idx, orig_size=orig_size)
    return ImageTk.PhotoImage(img)


class Card(Frame):
    """둥근 느낌의 flat 카드."""

    def __init__(self, parent, **kwargs):
        padx = kwargs.pop("padx", 0)
        pady = kwargs.pop("pady", 0)
        super().__init__(
            parent,
            bg=COLORS["bg_card"],
            highlightbackground=COLORS["bg_card"],
            highlightthickness=0,
            padx=padx,
            pady=pady,
            **kwargs,
        )


_ROUND_CACHE: dict[tuple, ImageTk.PhotoImage] = {}


def rounded_image(w: int, h: int, radius: int, fill: str, surface: str) -> ImageTk.PhotoImage:
    """둥근 사각형 배경 이미지 — surface 위에 fill 을 얹은 형태 (안티에일리어싱)."""
    w, h = max(int(w), 2), max(int(h), 2)
    radius = max(0, min(int(radius), w // 2, h // 2))
    key = (w, h, radius, fill, surface)
    cached = _ROUND_CACHE.get(key)
    if cached is not None:
        return cached
    scale = 4
    img = Image.new("RGB", (w * scale, h * scale), surface)
    draw = ImageDraw.Draw(img)
    box = (0, 0, w * scale - 1, h * scale - 1)
    try:
        draw.rounded_rectangle(box, radius=radius * scale, fill=fill)
    except AttributeError:
        draw.rectangle(box, fill=fill)
    photo = ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))
    _ROUND_CACHE[key] = photo
    return photo


# variant -> (배경, 글자색, hover 배경)
PILL_VARIANTS = {
    "primary": ("accent", "bg_root", "accent_hover"),
    "secondary": ("bg_card_alt", "text_primary", "border"),
    "ghost": ("accent_soft", "accent", "border"),
    "danger": ("stop_bg", "text_secondary", "stop_active"),
    "stop_on": ("stop_on_bg", "text_light", "stop_on_hover"),
    "stop_off": ("stop_off_bg", "stop_off_fg", "stop_off_bg"),
    "muted": ("bg_cell", "text_dim", "bg_cell"),
    "segment_off": ("bg_cell", "text_secondary", "bg_card_alt"),
}


class PillButton(Frame):
    """One UI 알약 버튼 — Tk Button 대신 Label + 둥근 PIL 이미지.

    Tk Button 은 비활성 시 격자 무늬를 씌우고 위젯 자체가 네모라
    라운드 이미지가 있어도 각이 남는다. Label 로 그려 테두리를 없앤다.
    """

    def __init__(
        self, parent, text, command=None, *, variant="secondary", font=FONT_BODY,
        padx=20, pady=11, width=None, surface=None, radius=None,
        disabled_variant="muted",
    ):
        self._surface = surface or parent.cget("bg")
        super().__init__(parent, bg=self._surface, highlightthickness=0, bd=0)
        self._label_text = text
        self._font_spec = font
        self._padx = padx
        self._pady = pady
        self._fixed_width = width
        self._radius = radius
        self._variant = variant
        self._disabled_variant = disabled_variant
        self._command = command
        self._enabled = True
        self._pressed = False
        self._photo = None
        self._hover_photo = None
        self._fg = COLORS["text_primary"]
        self._lbl = Label(
            self, text=text, font=font, compound="center",
            bg=self._surface, bd=0, highlightthickness=0,
        )
        self._lbl.pack()
        self._render()
        for w in (self, self._lbl):
            w.bind("<ButtonPress-1>", self._on_press)
            w.bind("<ButtonRelease-1>", self._on_release)
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)

    def _invoke(self):
        if self._enabled and self._command:
            self._command()

    def _on_press(self, _event=None):
        self._pressed = True

    def _on_release(self, _event=None):
        if not getattr(self, "_pressed", False):
            return
        self._pressed = False
        self._invoke()

    def _on_enter(self, _event=None):
        if self._enabled and self._hover_photo is not None:
            self._lbl.config(image=self._hover_photo, cursor="hand2")

    def _on_leave(self, _event=None):
        self._lbl.config(image=self._photo, cursor="hand2" if self._enabled else "arrow")

    def _metrics(self) -> tuple[int, int]:
        f = tkfont.Font(font=self._font_spec)
        text_w = max(f.measure(line) for line in self._label_text.split("\n"))
        line_h = f.metrics("linespace") * len(self._label_text.split("\n"))
        w = self._fixed_width or text_w + self._padx * 2
        return int(w), int(line_h + self._pady * 2)

    def _render(self):
        variant = self._variant if self._enabled else self._disabled_variant
        fill_key, fg_key, hover_key = PILL_VARIANTS.get(variant, PILL_VARIANTS["secondary"])
        w, h = self._metrics()
        radius = self._radius if self._radius is not None else h // 2
        self._photo = rounded_image(w, h, radius, COLORS[fill_key], self._surface)
        self._hover_photo = rounded_image(w, h, radius, COLORS[hover_key], self._surface)
        self._fg = COLORS[fg_key]
        self._lbl.config(
            image=self._photo, text=self._label_text, fg=self._fg,
            bg=self._surface, cursor="hand2" if self._enabled else "arrow",
        )

    def set_variant(self, variant: str):
        self._variant = variant
        self._render()

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        self._render()

    def set_text(self, text: str):
        self._label_text = text
        self._render()


class GlassCard(Frame):
    """레이어드 패널 — 대시보드 카드 느낌."""

    def __init__(self, parent, *, padx=14, pady=14, outer_pad=6):
        super().__init__(parent, bg=COLORS["bg_root"])
        shell = Frame(self, bg=COLORS["bg_root"], padx=outer_pad, pady=outer_pad)
        shell.pack(fill="both", expand=True)
        self.body = Frame(
            shell,
            bg=COLORS["bg_glass"],
            highlightthickness=0,
            padx=padx,
            pady=pady,
        )
        self.body.pack(fill="both", expand=True)


def _pill_group(parent, options: tuple[tuple[str, str], ...], command, active_key: str):
    """세그먼트 pill 버튼 그룹. options: (key, label)."""
    wrap = Frame(parent, bg=COLORS["bg_cell"], padx=4, pady=4)
    buttons = {}
    for key, label in options:
        btn = PillButton(
            wrap, label, lambda k=key: command(k),
            variant="primary" if key == active_key else "segment_off",
            padx=18, pady=8, surface=COLORS["bg_cell"],
        )
        btn.pack(side="left", padx=2)
        buttons[key] = btn
    return wrap, buttons


def _set_segment_active(buttons: dict, active_key: str):
    for key, btn in buttons.items():
        btn.set_variant("primary" if key == active_key else "segment_off")


def _kpi_chip(parent, title: str, accent: str):
    box = Frame(parent, bg=COLORS["bg_glass"], highlightthickness=0)
    box.pack(side="left", fill="x", expand=True, padx=5)
    inner = Frame(box, bg=COLORS["bg_glass"], padx=16, pady=14)
    inner.pack(fill="both", expand=True)
    row = Frame(inner, bg=COLORS["bg_glass"])
    row.pack(fill="x")
    Frame(row, bg=accent, width=3, height=34).pack(side="left", fill="y", padx=(0, 12))
    content = Frame(row, bg=COLORS["bg_glass"])
    content.pack(side="left", fill="both", expand=True)
    Label(content, text=title, font=FONT_SMALL, fg=COLORS["text_dim"], bg=COLORS["bg_glass"]).pack(anchor="w")
    var = StringVar(value="—")
    Label(content, textvariable=var, font=FONT_STAT, fg=COLORS["text_primary"], bg=COLORS["bg_glass"]).pack(anchor="w", pady=(1, 0))
    return var


class AppHeader(Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=COLORS["bg_root"], padx=18, pady=14)
        self.pack(fill="x")

        brand = Frame(self, bg=COLORS["bg_root"])
        brand.pack(side="left")
        Label(
            brand, text="DR", font=("Segoe UI", 11, "bold"),
            fg=COLORS["accent"], bg=COLORS["accent_soft"], padx=9, pady=4,
        ).pack(side="left")
        Label(brand, text="  Darkroom Vision", font=FONT_TITLE, fg=COLORS["text_primary"], bg=COLORS["bg_root"]).pack(side="left")

        right = Frame(self, bg=COLORS["bg_root"])
        right.pack(side="right")
        self.clock_var = __import__("tkinter").StringVar()
        Label(right, textvariable=self.clock_var, font=FONT_BODY, fg=COLORS["text_secondary"], bg=COLORS["bg_root"]).pack(side="right", padx=(12, 0))
        self.link_var = __import__("tkinter").StringVar(value="실행기 대기")
        self.dot = Label(right, text="●", font=("Segoe UI", 11), fg=COLORS["text_dim"], bg=COLORS["bg_root"])
        self.dot.pack(side="right", padx=(0, 4))
        Label(right, textvariable=self.link_var, font=FONT_SMALL, fg=COLORS["text_secondary"], bg=COLORS["bg_root"]).pack(side="right")
        self._tick()

    def _tick(self):
        self.clock_var.set(datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
        self.after(1000, self._tick)

    def set_link(self, text, connected=False):
        self.link_var.set(text)
        self.dot.config(fg=COLORS["ok"] if connected else COLORS["text_dim"])


class Sidebar(Frame):
    NAV_ITEMS = (("●", "검사"), ("◆", "기록"), ("▣", "리포트"), ("⚙", "설정"))

    def __init__(self, parent, on_nav=None):
        super().__init__(parent, bg=COLORS["bg_sidebar"], width=80)
        self.pack(side="left", fill="y")
        self.pack_propagate(False)
        self.on_nav = on_nav
        self._active = "검사"
        self._boxes = {}
        for icon, name in self.NAV_ITEMS:
            cell = Frame(self, bg=COLORS["bg_sidebar"], pady=5, cursor="hand2")
            cell.pack(fill="x")
            box = Frame(cell, bg=COLORS["bg_sidebar"], padx=4, pady=11)
            box.pack(fill="x", padx=10)
            icon_lbl = Label(box, text=icon, font=("Segoe UI", 14), bg=COLORS["bg_sidebar"])
            icon_lbl.pack()
            name_lbl = Label(box, text=name, font=("Segoe UI", 8), bg=COLORS["bg_sidebar"])
            name_lbl.pack(pady=(3, 0))
            self._boxes[name] = (cell, box, icon_lbl, name_lbl)
            for widget in (cell, box, icon_lbl, name_lbl):
                widget.bind("<Button-1>", lambda _e, n=name: self._click(n))
        self.set_active("검사")

    def _click(self, name):
        if self.on_nav:
            self.on_nav(name)

    def set_active(self, name):
        self._active = name
        for item_name, (_cell, box, icon_lbl, name_lbl) in self._boxes.items():
            active = item_name == name
            fg = COLORS["accent"] if active else COLORS["text_dim"]
            bg = COLORS["accent_soft"] if active else COLORS["bg_sidebar"]
            box.config(bg=bg)
            icon_lbl.config(fg=fg, bg=bg)
            name_lbl.config(fg=fg, bg=bg)


class RecordDetailPanel(Frame):
    """선택한 샘플 기록 — 확대 미리보기 + 불량 위치 (스크롤 가능)."""

    def __init__(self, parent):
        super().__init__(parent, bg=COLORS["bg_root"])
        self._record = None
        self._photos = {}

        shell = Card(self, padx=0, pady=0)
        shell.pack(fill="both", expand=True)
        self._scroll = ScrollableFrame(shell, bg=COLORS["bg_card"], pad_right=4)
        self._scroll.pack(fill="both", expand=True, padx=8, pady=8)
        body = self._scroll.content

        self.title_var = StringVar(value="샘플을 선택하세요")
        Label(
            body, textvariable=self.title_var, font=FONT_TITLE,
            fg=COLORS["text_primary"], bg=COLORS["bg_card"], anchor="w",
        ).pack(fill="x", padx=4, pady=(4, 0))

        self.meta_var = StringVar(value="왼쪽 폴더 카드를 클릭하면 세부 정보가 표시됩니다")
        Label(
            body, textvariable=self.meta_var, font=FONT_SMALL,
            fg=COLORS["text_secondary"], bg=COLORS["bg_card"], anchor="w",
            wraplength=560, justify="left",
        ).pack(fill="x", padx=4, pady=(6, 12))

        self.verdict_frame = Frame(
            body, bg=COLORS["bg_cell"],
            highlightbackground=COLORS["border_soft"], highlightthickness=1,
        )
        self.verdict_frame.pack(fill="x", padx=4, pady=(0, 12))
        inner_verdict = Frame(self.verdict_frame, bg=COLORS["bg_cell"], pady=14)
        inner_verdict.pack(fill="x")
        self.verdict_var = StringVar(value="—")
        self.verdict_label = Label(
            inner_verdict, textvariable=self.verdict_var, font=("Segoe UI", 32, "bold"),
            fg=COLORS["text_dim"], bg=COLORS["bg_cell"],
        )
        self.verdict_label.pack()
        self.verdict_sub = StringVar(value="")
        self.verdict_sub_label = Label(
            inner_verdict, textvariable=self.verdict_sub, font=FONT_SMALL,
            fg=COLORS["text_secondary"], bg=COLORS["bg_cell"],
        )
        self.verdict_sub_label.pack(pady=(4, 0))

        _section_label(body, "미리보기", subtitle="샘플 6면 그리드 요약")
        preview_card = Frame(body, bg=COLORS["bg_cell"], highlightthickness=0)
        preview_card.pack(fill="x", padx=4, pady=(0, 4))
        self.preview_label = Label(
            preview_card, text="미리보기", bg=COLORS["bg_cell"], fg=COLORS["text_dim"],
        )
        self.preview_label.pack(fill="x", padx=10, pady=10)

        _section_label(body, "검사 영상", subtitle=f"총 {INSPECT_FACE_TOTAL}면")
        self.wall_host = Frame(body, bg=COLORS["bg_card"])
        self.wall_host.pack(fill="x", padx=4)
        self.inspect_wall = InspectWall(self.wall_host, cell_size=(228, 128), auto_pack=False, show_header=False)
        self.inspect_wall.pack(fill="x")

        _section_label(body, "비지도")
        defect_block = Frame(body, bg=COLORS["bg_cell"], highlightthickness=0)
        defect_block.pack(fill="x", padx=4, pady=(0, 4))
        defect_head = Frame(defect_block, bg=COLORS["bg_cell"])
        defect_head.pack(fill="x", padx=10, pady=(8, 4))
        self.unsup_summary = StringVar(value="")
        self.defect_summary = self.unsup_summary
        Label(
            defect_head, textvariable=self.unsup_summary, font=FONT_SMALL,
            fg=COLORS["text_dim"], bg=COLORS["bg_cell"], anchor="w",
        ).pack(fill="x")
        self.unsup_list = Listbox(
            defect_block, font=FONT_BODY, height=6,
            bg=COLORS["bg_root"], fg=COLORS["text_primary"],
            selectbackground=COLORS["accent_soft"], selectforeground=COLORS["accent"],
            relief="flat", borderwidth=0, highlightthickness=0,
            activestyle="none",
        )
        self.unsup_list.pack(fill="x", padx=10, pady=(0, 8))
        self.unsup_list.bind("<<ListboxSelect>>", self._on_defect_select)
        self.defect_list = self.unsup_list

        _section_label(body, "YOLO")
        yolo_block = Frame(body, bg=COLORS["bg_cell"], highlightthickness=0)
        yolo_block.pack(fill="x", padx=4, pady=(0, 4))
        yolo_head = Frame(yolo_block, bg=COLORS["bg_cell"])
        yolo_head.pack(fill="x", padx=10, pady=(8, 4))
        self.yolo_summary = StringVar(value="")
        Label(
            yolo_head, textvariable=self.yolo_summary, font=FONT_SMALL,
            fg=COLORS["text_dim"], bg=COLORS["bg_cell"], anchor="w",
        ).pack(fill="x")
        self.yolo_list = Listbox(
            yolo_block, font=FONT_BODY, height=5,
            bg=COLORS["bg_root"], fg=COLORS["text_primary"],
            selectbackground=COLORS["accent_soft"], selectforeground=COLORS["accent"],
            relief="flat", borderwidth=0, highlightthickness=0,
            activestyle="none",
        )
        self.yolo_list.pack(fill="x", padx=10, pady=(0, 8))
        self.yolo_list.bind("<<ListboxSelect>>", self._on_defect_select)

        btn_row = Frame(body, bg=COLORS["bg_card"])
        btn_row.pack(fill="x", padx=4, pady=(16, 12))
        self.load_btn = PillButton(
            btn_row, "검사 화면에 불러오기", self._emit_load, variant="primary",
        )
        self.load_btn.set_enabled(False)
        self.load_btn.pack(side="left")

        self.on_load = None

    def bind_load(self, callback):
        self.on_load = callback

    def _emit_load(self):
        if self._record and self.on_load:
            self.on_load(self._record)

    def clear(self):
        self._record = None
        self.title_var.set("샘플을 선택하세요")
        self.meta_var.set("왼쪽 폴더 카드를 클릭하면 세부 정보가 표시됩니다")
        self.verdict_var.set("—")
        self.verdict_sub.set("")
        self.verdict_frame.config(bg=COLORS["bg_cell"], highlightbackground=COLORS["border_soft"])
        inner = self.verdict_frame.winfo_children()[0]
        inner.config(bg=COLORS["bg_cell"])
        self.verdict_label.config(bg=COLORS["bg_cell"], fg=COLORS["text_dim"])
        self.verdict_sub_label.config(bg=COLORS["bg_cell"])
        self.preview_label.config(image="", text="미리보기", fg=COLORS["text_dim"])
        self.unsup_summary.set("")
        self.yolo_summary.set("")
        self.unsup_list.delete(0, END)
        self.yolo_list.delete(0, END)
        self._unsup_items = []
        self._yolo_items = []
        self._list_items = []
        self.load_btn.set_enabled(False)
        self.inspect_wall.clear()

    def show_record(self, record):
        self._record = record
        judgment = record.get("judgment") or {}
        verdict = record.get("verdict", "—")
        defects = judgment.get("defects") or []

        judged = format_judged_at(record.get("judged_at", ""))
        folder = Path(record.get("archive_path", "")).name
        self.title_var.set(f"Run #{record.get('run_no')}  ·  {folder}")
        self.meta_var.set(
            f"판정 시각  {judged}\n"
            f"촬영  {INSPECT_FACE_TOTAL}면\n"
            f"백엔드  {record.get('backend', '—')}  ·  "
            f"불량 {record.get('defect_count', len(defects))}건"
        )
        if record.get("message"):
            self.meta_var.set(self.meta_var.get() + f"\n{record['message']}")

        unsup_items, yolo_items = split_judge_items(judgment)
        self.verdict_var.set(verdict)
        if verdict == "OK":
            bg, fg, edge = COLORS["ok_bg"], COLORS["ok"], COLORS["ok"]
            self.verdict_sub.set("비지도 · YOLO 통과" if yolo_items or unsup_items else "불량 없음")
        elif verdict == "NG":
            bg, fg, edge = COLORS["ng_bg"], COLORS["ng"], COLORS["ng"]
            parts = []
            unsup_ng = sum(1 for item in unsup_items if item.get("ng"))
            if unsup_items:
                parts.append(f"비지도 {unsup_ng}면")
            if yolo_items:
                parts.append(f"YOLO {yolo_category_summary(yolo_items)}")
            self.verdict_sub.set(" · ".join(parts) or f"불량 {len(defects)}건")
        else:
            bg, fg, edge = COLORS["bg_cell"], COLORS["text_dim"], COLORS["border_soft"]
            self.verdict_sub.set("")
        self.verdict_frame.config(bg=bg, highlightbackground=edge)
        inner = self.verdict_frame.winfo_children()[0]
        inner.config(bg=bg)
        self.verdict_label.config(bg=bg, fg=fg)
        self.verdict_sub_label.config(bg=bg)
        self._scroll.scroll_to_top()

        preview = record.get("preview", "")
        if preview and Path(preview).is_file():
            try:
                img = Image.open(preview).convert("RGB")
                img.thumbnail((520, 220))
                photo = ImageTk.PhotoImage(img)
                self._photos["preview"] = photo
                self.preview_label.config(image=photo, text="")
            except OSError:
                self.preview_label.config(image="", text="미리보기 로드 실패", fg=COLORS["ng"])
        else:
            self.preview_label.config(image="", text="미리보기 없음", fg=COLORS["text_dim"])

        captures = {
            "1차": judgment.get("manifest_1", record.get("manifest_1", "")),
            "2차": judgment.get("manifest_2", record.get("manifest_2", "")),
        }
        self.inspect_wall.load_capture_folders(captures)
        self.inspect_wall.set_judgment(judgment)

        unsup_items, yolo_items = split_judge_items(judgment)
        self._unsup_items = unsup_items
        self._yolo_items = yolo_items
        self._list_items = unsup_items + yolo_items
        self.unsup_list.delete(0, END)
        self.yolo_list.delete(0, END)
        if unsup_items:
            ng_n = sum(1 for item in unsup_items if item.get("ng"))
            self.unsup_summary.set(f"{len(unsup_items)}면 · 이상 {ng_n}면 · 선택 시 위치 강조")
            for item in unsup_items:
                self.unsup_list.insert(END, defect_line(item))
        else:
            self.unsup_summary.set("이번 판정 없음")
        if yolo_items:
            self.yolo_summary.set(yolo_category_summary(yolo_items) + " · 선택 시 위치 강조")
            for item in yolo_items:
                self.yolo_list.insert(END, defect_line(item))
        else:
            self.yolo_summary.set("검출 없음  ·  스크래치 0 · 찌그러짐 0")
        self.load_btn.set_enabled(True)

    def _on_defect_select(self, event=None):
        widget = event.widget if event is not None else self.unsup_list
        sel = widget.curselection()
        if not sel:
            return
        if widget is self.yolo_list:
            items = getattr(self, "_yolo_items", None)
            self.unsup_list.selection_clear(0, END)
        else:
            items = getattr(self, "_unsup_items", None) or getattr(self, "_list_items", None)
            self.yolo_list.selection_clear(0, END)
        if items and 0 <= int(sel[0]) < len(items):
            self.inspect_wall.highlight_item(items[int(sel[0])])
        else:
            self.inspect_wall.highlight_defect(int(sel[0]))


class RecordsPage(Frame):
    """검사 기록 — 샘플별 폴더 카드 + 확대 상세."""

    CARD_W = 200

    def __init__(self, parent, on_load_record=None, on_back=None):
        super().__init__(parent, bg=COLORS["bg_root"])
        ensure_ui_theme(parent.winfo_toplevel())
        self.on_load_record = on_load_record
        self.on_back = on_back
        self._records = []
        self._filtered = []
        self._selected_id = None
        self._filter = "all"
        self._card_photos = {}
        self._card_frames = {}

        header = Frame(self, bg=COLORS["bg_root"], padx=12, pady=10)
        header.pack(fill="x")
        Label(header, text="검사 기록", font=FONT_TITLE, fg=COLORS["text_primary"], bg=COLORS["bg_root"]).pack(side="left")
        self.summary_var = __import__("tkinter").StringVar(value="샘플 1건 = 6면 · 폴더 1개")
        Label(
            header, textvariable=self.summary_var, font=FONT_SMALL,
            fg=COLORS["text_secondary"], bg=COLORS["bg_root"],
        ).pack(side="left", padx=(16, 0))

        stats = Frame(self, bg=COLORS["bg_root"], padx=12)
        stats.pack(fill="x", pady=(0, 8))
        self.stat_vars = {}
        stat_accents = {
            "Total": COLORS["accent"],
            "OK": COLORS["ok"],
            "NG": COLORS["ng"],
            "OK Rate": COLORS["warn"],
        }
        for title in ("Total", "OK", "NG", "OK Rate"):
            box = Frame(stats, bg=COLORS["bg_card"], highlightthickness=0)
            box.pack(side="left", fill="x", expand=True, padx=4)
            stripe = Frame(box, bg=stat_accents[title], width=3)
            stripe.pack(side="left", fill="y")
            inner = Frame(box, bg=COLORS["bg_card"], padx=16, pady=14)
            inner.pack(side="left", fill="both", expand=True)
            Label(inner, text=title, font=FONT_SMALL, fg=COLORS["text_dim"], bg=COLORS["bg_card"]).pack(anchor="w")
            var = StringVar(value="—")
            self.stat_vars[title] = var
            Label(inner, textvariable=var, font=FONT_STAT, fg=COLORS["text_primary"], bg=COLORS["bg_card"]).pack(anchor="w", pady=(2, 0))

        btn_row = Frame(self, bg=COLORS["bg_root"], padx=12)
        btn_row.pack(fill="x", pady=(0, 8))
        self._filter_wrap, self._filter_btns = _pill_group(
            btn_row,
            (("all", "전체"), ("OK", "OK"), ("NG", "NG")),
            self._set_filter,
            self._filter,
        )
        self._filter_wrap.pack(side="left")
        PillButton(btn_row, "↩ 검사 화면", self._go_back, variant="secondary").pack(side="left", padx=(12, 0))
        PillButton(btn_row, "새로고침", self.refresh, variant="secondary").pack(side="left", padx=(8, 0))
        PillButton(btn_row, "기록 전체 삭제", self._clear, variant="danger").pack(side="right")

        body = Frame(self, bg=COLORS["bg_root"])
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        body.columnconfigure(0, weight=2)
        body.columnconfigure(1, weight=3)
        body.rowconfigure(0, weight=1)

        grid_card = Card(body, padx=8, pady=8)
        grid_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        Label(grid_card, text="샘플 폴더", font=FONT_HEAD, fg=COLORS["text_primary"], bg=COLORS["bg_card"]).pack(anchor="w")
        Label(
            grid_card, text=f"저장 위치  {RECORDS_ROOT}", font=FONT_SMALL,
            fg=COLORS["text_dim"], bg=COLORS["bg_card"], anchor="w",
        ).pack(fill="x", pady=(2, 8))

        grid_wrap = Frame(grid_card, bg=COLORS["bg_card"])
        grid_wrap.pack(fill="both", expand=True)
        self.grid_canvas = Canvas(grid_wrap, bg=COLORS["bg_card"], highlightthickness=0, borderwidth=0)
        grid_scroll = ttk.Scrollbar(
            grid_wrap, orient="vertical", command=self.grid_canvas.yview, style="Dark.Vertical.TScrollbar",
        )
        self.grid_canvas.configure(yscrollcommand=grid_scroll.set)
        self.grid_canvas.pack(side="left", fill="both", expand=True)
        grid_scroll.pack(side="right", fill="y", padx=(4, 0))
        self.grid_inner = Frame(self.grid_canvas, bg=COLORS["bg_card"])
        self._grid_window = self.grid_canvas.create_window((0, 0), window=self.grid_inner, anchor="nw")
        self.grid_inner.bind("<Configure>", self._on_grid_configure)
        self.grid_canvas.bind("<Configure>", self._on_canvas_configure)
        for widget in (grid_wrap, self.grid_canvas, self.grid_inner):
            widget.bind("<Enter>", self._bind_grid_wheel)
            widget.bind("<Leave>", self._unbind_grid_wheel)

        self.detail = RecordDetailPanel(body)
        self.detail.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.detail.bind_load(self._load_current)

    def _on_grid_configure(self, _event=None):
        self.grid_canvas.configure(scrollregion=self.grid_canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.grid_canvas.itemconfigure(self._grid_window, width=event.width)

    def _bind_grid_wheel(self, _event=None):
        self.grid_canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.grid_canvas.bind_all("<Button-4>", self._on_mousewheel_linux)
        self.grid_canvas.bind_all("<Button-5>", self._on_mousewheel_linux)

    def _unbind_grid_wheel(self, _event=None):
        self.grid_canvas.unbind_all("<MouseWheel>")
        self.grid_canvas.unbind_all("<Button-4>")
        self.grid_canvas.unbind_all("<Button-5>")

    def _on_mousewheel_linux(self, event):
        if not self.winfo_ismapped():
            return
        self.grid_canvas.yview_scroll(-1 if event.num == 4 else 1, "units")

    def _on_mousewheel(self, event):
        if not self.winfo_ismapped():
            return
        widget = event.widget
        while widget:
            if widget is self.grid_canvas:
                self.grid_canvas.yview_scroll(int(-event.delta / 120), "units")
                break
            widget = widget.master

    def _go_back(self):
        if self.on_back:
            self.on_back()

    def _load_current(self, record):
        if self.on_load_record:
            self.on_load_record(record)

    def _set_filter(self, key):
        self._filter = key
        _set_segment_active(self._filter_btns, key)
        self._apply_filter()
        self._render_cards()

    def _apply_filter(self):
        if self._filter == "all":
            self._filtered = list(self._records)
        else:
            self._filtered = [r for r in self._records if r.get("verdict") == self._filter]

    def _render_cards(self):
        for child in self.grid_inner.winfo_children():
            child.destroy()
        self._card_photos.clear()
        self._card_frames.clear()

        if not self._filtered:
            msg = "해당 조건의 샘플이 없습니다." if self._records else (
                "아직 저장된 샘플이 없습니다.\n연속 검사가 끝나면 여기에 쌓입니다."
            )
            Label(
                self.grid_inner, text=msg,
                font=FONT_BODY, fg=COLORS["text_dim"], bg=COLORS["bg_card"], justify="left",
            ).pack(anchor="w", padx=8, pady=12)
            self.detail.clear()
            return

        canvas_w = self.grid_canvas.winfo_width() or 420
        cols = max(1, canvas_w // (self.CARD_W + 12))
        for idx, rec in enumerate(self._filtered):
            row, col = divmod(idx, cols)
            card = self._make_card(self.grid_inner, rec)
            card.grid(row=row, column=col, padx=6, pady=6, sticky="n")

        selected = next((r for r in self._filtered if r.get("id") == self._selected_id), None)
        if selected is None:
            selected = self._filtered[0]
        self._select_record(selected, scroll=False)

    def refresh(self):
        self._records = list_records()
        summary = history_summary(self._records)
        self.stat_vars["Total"].set(str(summary["total"]))
        self.stat_vars["OK"].set(str(summary["ok"]))
        self.stat_vars["NG"].set(str(summary["ng"]))
        self.stat_vars["OK Rate"].set(summary["rate"])
        self.summary_var.set(
            f"샘플 {summary['total']}건 · 6면/건 · {RECORDS_ROOT.name}/"
        )
        self._apply_filter()
        self._render_cards()

    def _make_card(self, parent, record):
        rec_id = record.get("id")
        verdict = record.get("verdict", "—")
        badge_fg = COLORS["ok"] if verdict == "OK" else COLORS["ng"] if verdict == "NG" else COLORS["text_dim"]
        border = COLORS["border_active"] if rec_id == self._selected_id else COLORS["bg_glass"]

        card = Frame(
            parent, bg=COLORS["bg_glass"], cursor="hand2",
            highlightbackground=border, highlightthickness=2 if rec_id == self._selected_id else 1,
            width=self.CARD_W,
        )
        self._card_frames[rec_id] = card

        status_bar = Frame(card, bg=badge_fg if verdict in ("OK", "NG") else COLORS["border"], height=3)
        status_bar.pack(fill="x")
        status_bar.pack_propagate(False)

        thumb = Frame(card, bg=COLORS["bg_cell"], height=112)
        thumb.pack(fill="x", padx=8, pady=(8, 6))
        thumb.pack_propagate(False)
        img_label = Label(thumb, bg=COLORS["bg_cell"], fg=COLORS["text_dim"], text="—")
        img_label.place(relx=0, rely=0, relwidth=1, relheight=1)

        preview = record.get("preview", "")
        if preview and Path(preview).is_file():
            try:
                img = Image.open(preview).convert("RGB")
                img.thumbnail((self.CARD_W - 20, 108))
                photo = ImageTk.PhotoImage(img)
                self._card_photos[rec_id] = photo
                img_label.config(image=photo, text="")
            except OSError:
                img_label.config(text="미리보기")

        pill_bg = COLORS["ok_bg"] if verdict == "OK" else COLORS["ng_bg"] if verdict == "NG" else COLORS["bg_card_alt"]
        pill = Label(
            thumb, text=verdict, font=("Segoe UI", 9, "bold"),
            fg=badge_fg, bg=pill_bg, padx=8, pady=2,
        )
        pill.place(relx=1.0, rely=0.0, anchor="ne", x=-4, y=4)

        info = Frame(card, bg=COLORS["bg_glass"])
        info.pack(fill="x", padx=8, pady=(0, 8))
        Label(
            info, text=format_judged_at(record.get("judged_at", "")),
            font=FONT_BODY, fg=COLORS["text_primary"], bg=COLORS["bg_glass"],
        ).pack(anchor="w")
        Label(
            info, text=f"Run #{record.get('run_no')}  ·  {INSPECT_FACE_TOTAL}면  ·  불량 {record.get('defect_count', 0)}",
            font=FONT_SMALL, fg=COLORS["text_secondary"], bg=COLORS["bg_glass"],
        ).pack(anchor="w", pady=(2, 0))
        folder_name = Path(record.get("archive_path", "")).name
        if folder_name:
            Label(
                info, text=folder_name, font=("Segoe UI", 8), fg=COLORS["text_dim"], bg=COLORS["bg_glass"],
                wraplength=self.CARD_W - 16, justify="left",
            ).pack(anchor="w", pady=(4, 0))
        if record.get("backend") == "demo":
            Label(
                info, text="DEMO", font=("Segoe UI", 7, "bold"), fg=COLORS["accent"], bg=COLORS["accent_soft"],
                padx=6, pady=1,
            ).pack(anchor="w", pady=(4, 0))

        def bind_click(widget):
            widget.bind("<Button-1>", lambda _e, r=record: self._select_record(r))

        bind_click(card)
        for widget in (card, thumb, img_label, pill, info):
            bind_click(widget)
        for child in info.winfo_children():
            bind_click(child)
        return card

    def _select_record(self, record, scroll=True):
        self._selected_id = record.get("id")
        for rec_id, frame in self._card_frames.items():
            selected = rec_id == self._selected_id
            frame.config(
                highlightbackground=COLORS["border_active"] if selected else COLORS["bg_glass"],
                highlightthickness=2 if selected else 1,
            )
        self.detail.show_record(record)
        if scroll and record.get("id") in self._card_frames:
            frame = self._card_frames[record["id"]]
            self.grid_canvas.update_idletasks()
            y = frame.winfo_y()
            self.grid_canvas.yview_moveto(max(0, (y - 20) / max(1, self.grid_inner.winfo_height())))

    def _clear(self):
        import tkinter.messagebox as mb
        if not self._records:
            return
        if mb.askyesno(
            "기록 전체 삭제",
            f"저장된 샘플 폴더 {len(self._records)}건과 이미지를 모두 삭제할까요?\n\n{RECORDS_ROOT}",
        ):
            clear_history()
            self._selected_id = None
            self.refresh()


class ReportsPage(Frame):
    """통계 및 리포트 — QC 분석 (수율·SPC·불량 유형·NG 로그)."""

    def __init__(self, parent, on_back=None, on_open_record=None):
        super().__init__(parent, bg=COLORS["bg_root"])
        ensure_ui_theme(parent.winfo_toplevel())
        self.on_back = on_back
        self.on_open_record = on_open_record
        self._period = "day"
        self._photos: dict[str, Any] = {}
        self._ng_rows: list[dict] = []

        header = Frame(self, bg=COLORS["bg_root"])
        header.pack(fill="x", padx=16, pady=(12, 6))
        title_col = Frame(header, bg=COLORS["bg_root"])
        title_col.pack(side="left", fill="y")
        Label(title_col, text="통계 및 리포트", font=("Segoe UI", 18, "bold"), fg=COLORS["text_primary"], bg=COLORS["bg_root"]).pack(anchor="w")
        Label(
            title_col, text="Quality Control · SPC · 불량 분석",
            font=FONT_SMALL, fg=COLORS["text_secondary"], bg=COLORS["bg_root"],
        ).pack(anchor="w", pady=(4, 0))

        ctrl = Frame(header, bg=COLORS["bg_root"])
        ctrl.pack(side="right")
        self._period_wrap, self._period_btns = _pill_group(
            ctrl,
            (("day", "일"), ("week", "주"), ("month", "월")),
            self._set_period,
            self._period,
        )
        self._period_wrap.pack(side="left", padx=(0, 10))
        PillButton(ctrl, "↻", self.refresh, variant="ghost", padx=15).pack(side="left", padx=2)
        PillButton(ctrl, "검사 화면", self._go_back, variant="secondary").pack(side="left", padx=(6, 0))

        kpi = Frame(self, bg=COLORS["bg_root"], padx=16)
        kpi.pack(fill="x", pady=(4, 10))
        self._kpi_total = _kpi_chip(kpi, "Total", COLORS["accent"])
        self._kpi_ok = _kpi_chip(kpi, "OK Rate", COLORS["ok"])
        self._kpi_ng = _kpi_chip(kpi, "NG", COLORS["ng"])
        self._kpi_types = _kpi_chip(kpi, "불량 유형", COLORS["warn"])

        scroll_shell = Frame(self, bg=COLORS["bg_root"])
        scroll_shell.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._scroll = ScrollableFrame(scroll_shell, bg=COLORS["bg_root"], pad_right=2)
        self._scroll.pack(fill="both", expand=True)
        body = self._scroll.content

        charts_row = Frame(body, bg=COLORS["bg_root"])
        charts_row.pack(fill="x", pady=(0, 10))
        charts_row.columnconfigure(0, weight=1)
        charts_row.columnconfigure(1, weight=1)

        yield_wrap = GlassCard(charts_row, padx=6, pady=6, outer_pad=2)
        yield_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self._yield_lbl = Label(yield_wrap.body, bg=COLORS["bg_glass"])
        self._yield_lbl.pack(fill="both", expand=True)

        pie_wrap = GlassCard(charts_row, padx=6, pady=6, outer_pad=2)
        pie_wrap.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        self._pie_lbl = Label(pie_wrap.body, bg=COLORS["bg_glass"])
        self._pie_lbl.pack(fill="both", expand=True)

        spc_row = Frame(body, bg=COLORS["bg_root"])
        spc_row.pack(fill="x", pady=(0, 10))
        spc_row.columnconfigure(0, weight=1)
        spc_row.columnconfigure(1, weight=1)

        xbar_wrap = GlassCard(spc_row, padx=6, pady=6, outer_pad=2)
        xbar_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self._xbar_lbl = Label(xbar_wrap.body, bg=COLORS["bg_glass"])
        self._xbar_lbl.pack(fill="both", expand=True)

        r_wrap = GlassCard(spc_row, padx=6, pady=6, outer_pad=2)
        r_wrap.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        self._r_lbl = Label(r_wrap.body, bg=COLORS["bg_glass"])
        self._r_lbl.pack(fill="both", expand=True)

        log_header = Frame(body, bg=COLORS["bg_root"])
        log_header.pack(fill="x", pady=(4, 8))
        Label(log_header, text="NG 히스토리", font=FONT_HEAD, fg=COLORS["text_primary"], bg=COLORS["bg_root"]).pack(side="left")
        Label(
            log_header, text="  최근 불량 · 클릭하여 상세",
            font=FONT_SMALL, fg=COLORS["text_dim"], bg=COLORS["bg_root"],
        ).pack(side="left")

        log_body = Frame(body, bg=COLORS["bg_root"])
        log_body.pack(fill="x", pady=(0, 10))
        log_body.columnconfigure(0, weight=3)
        log_body.columnconfigure(1, weight=2)

        self._ng_host = Frame(log_body, bg=COLORS["bg_root"])
        self._ng_host.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        self._detail_card = GlassCard(log_body, padx=14, pady=14, outer_pad=2)
        self._detail_card.grid(row=0, column=1, sticky="nsew")
        detail = self._detail_card.body
        self._detail_title = StringVar(value="NG 상세")
        Label(
            detail, textvariable=self._detail_title, font=FONT_HEAD,
            fg=COLORS["text_primary"], bg=COLORS["bg_glass"], anchor="w",
        ).pack(fill="x")
        self._detail_meta = StringVar(value="목록에서 항목을 선택하세요")
        Label(
            detail, textvariable=self._detail_meta, font=FONT_SMALL,
            fg=COLORS["text_secondary"], bg=COLORS["bg_glass"], anchor="w", justify="left",
        ).pack(fill="x", pady=(6, 10))
        preview_frame = Frame(detail, bg=COLORS["bg_cell"], highlightthickness=0)
        preview_frame.pack(fill="x")
        self._detail_preview = Label(preview_frame, bg=COLORS["bg_cell"], fg=COLORS["text_dim"], text="—", pady=40)
        self._detail_preview.pack(fill="x", padx=8, pady=8)
        PillButton(
            detail, "기록 상세 보기 →", self._open_selected_record,
            variant="primary", surface=COLORS["bg_glass"],
        ).pack(anchor="w", pady=(12, 0))
        self._selected_ng: dict | None = None

    def _go_back(self):
        if self.on_back:
            self.on_back()

    def _set_period(self, key: str):
        self._period = key
        _set_segment_active(self._period_btns, key)
        self.refresh()

    def _set_chart(self, label: Label, key: str, pil_image):
        from PIL import ImageTk
        photo = ImageTk.PhotoImage(pil_image)
        self._photos[key] = photo
        label.config(image=photo)

    def refresh(self, scroll=True):
        from reports_analytics import build_report_bundle

        bundle = build_report_bundle(self._period)
        s = bundle["summary"]
        total = s["total"]
        ng = s["ng"]
        ok = total - ng
        rate = f"{ok / total * 100:.1f}%" if total else "—"
        self._kpi_total.set(str(total))
        self._kpi_ok.set(rate)
        self._kpi_ng.set(str(ng))
        self._kpi_types.set(str(s["defect_types"]))
        self._set_chart(self._yield_lbl, "yield", bundle["yield_chart"])
        self._set_chart(self._pie_lbl, "pie", bundle["pie_chart"])
        self._set_chart(self._xbar_lbl, "xbar", bundle["xbar_chart"])
        self._set_chart(self._r_lbl, "r", bundle["r_chart"])
        self._render_ng_log(bundle["ng_log"])
        if bundle["ng_log"]:
            self._select_ng(0, bundle["ng_log"][0])
        elif hasattr(self, "_detail_title"):
            self._detail_title.set("NG 상세")
            self._detail_meta.set("NG 기록이 없습니다")
            self._detail_preview.config(image="", text="—")
            self._selected_ng = None
        if scroll:
            self._scroll.scroll_to_top()

    def _render_ng_log(self, items: list[dict]):
        for child in self._ng_host.winfo_children():
            child.destroy()
        self._ng_rows = items
        if not items:
            Label(
                self._ng_host, text="NG 기록이 없습니다.",
                font=FONT_BODY, fg=COLORS["text_dim"], bg=COLORS["bg_root"],
            ).pack(anchor="w", padx=8, pady=8)
            return

        for idx, item in enumerate(items):
            row = Frame(
                self._ng_host, bg=COLORS["bg_glass"],
                highlightbackground=COLORS["bg_glass"], highlightthickness=1,
                cursor="hand2",
            )
            row.pack(fill="x", pady=5)
            accent = Frame(row, bg=COLORS["ng"], width=4)
            accent.pack(side="left", fill="y")
            accent.pack_propagate(False)

            inner = Frame(row, bg=COLORS["bg_glass"])
            inner.pack(side="left", fill="both", expand=True)

            thumb = Label(inner, bg=COLORS["bg_cell"], width=108, height=62)
            thumb.pack(side="left", padx=(10, 8), pady=10)
            preview = item.get("preview", "")
            if preview and Path(preview).is_file():
                try:
                    img = Image.open(preview).convert("RGB")
                    img.thumbnail((108, 62))
                    photo = ImageTk.PhotoImage(img)
                    self._photos[f"ng_{idx}"] = photo
                    thumb.config(image=photo, width=108, height=62)
                except OSError:
                    thumb.config(text="—")

            info = Frame(inner, bg=COLORS["bg_glass"])
            info.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=10)
            Label(
                info, text=item.get("judged_label", ""), font=FONT_BODY,
                fg=COLORS["text_primary"], bg=COLORS["bg_glass"], anchor="w",
            ).pack(fill="x")
            Label(
                info, text=f"Run #{item.get('run_no')}",
                font=FONT_SMALL, fg=COLORS["text_secondary"], bg=COLORS["bg_glass"], anchor="w",
            ).pack(fill="x", pady=(2, 0))
            Label(
                info, text=item.get("defect_types", "—"),
                font=FONT_SMALL, fg=COLORS["ng"], bg=COLORS["bg_glass"], anchor="w",
            ).pack(fill="x", pady=(4, 0))
            if item.get("message"):
                Label(
                    info, text=item["message"], font=("Segoe UI", 8),
                    fg=COLORS["text_dim"], bg=COLORS["bg_glass"], anchor="w",
                ).pack(fill="x", pady=(2, 0))

            def bind_click(widget, i=idx, it=item):
                widget.bind("<Button-1>", lambda _e: self._select_ng(i, it))

            bind_click(row)
            bind_click(inner)
            bind_click(accent)
            bind_click(thumb)
            bind_click(info)
            for child in info.winfo_children():
                bind_click(child)

    def _select_ng(self, idx: int, item: dict):
        self._selected_ng = item
        rec = item.get("record") or {}
        self._detail_title.set(f"Run #{item.get('run_no')}  ·  NG 상세")
        self._detail_meta.set(
            f"발생  {item.get('judged_label', '')}\n"
            f"제품  {item.get('product', '')}\n"
            f"불량 유형  {item.get('defect_types', '—')}"
        )
        preview = item.get("preview", "")
        if preview and Path(preview).is_file():
            try:
                img = Image.open(preview).convert("RGB")
                img.thumbnail((560, 220))
                photo = ImageTk.PhotoImage(img)
                self._photos["detail"] = photo
                self._detail_preview.config(image=photo, text="")
            except OSError:
                self._detail_preview.config(image="", text="미리보기 없음")
        else:
            self._detail_preview.config(image="", text="미리보기 없음")

        for i, child in enumerate(self._ng_host.winfo_children()):
            if isinstance(child, Frame):
                child.config(
                    highlightbackground=COLORS["border_active"] if i == idx else COLORS["bg_glass"],
                    highlightthickness=2 if i == idx else 1,
                )
                for sub in child.winfo_children():
                    if isinstance(sub, Frame) and sub.winfo_width() == 4:
                        sub.config(bg=COLORS["accent"] if i == idx else COLORS["ng"])

    def _open_selected_record(self):
        if self._selected_ng and self.on_open_record:
            rec = self._selected_ng.get("record")
            if rec:
                self.on_open_record(rec)


class SettingsPage(Frame):
    """시스템 설정 — 판정·장치·경로."""

    BACKENDS = ("unsup", "yolo", "both", "stub", "mock", "model")

    def __init__(self, parent, on_back=None):
        super().__init__(parent, bg=COLORS["bg_root"])
        self.on_back = on_back
        self._fields = {}

        header = Frame(self, bg=COLORS["bg_root"], padx=12, pady=10)
        header.pack(fill="x")
        Label(header, text="설정", font=FONT_TITLE, fg=COLORS["text_primary"], bg=COLORS["bg_root"]).pack(side="left")
        Label(
            header,
            text="판정 설정은 저장 후 실행기(robot_client) 재시작 시 반영됩니다",
            font=FONT_SMALL, fg=COLORS["text_dim"], bg=COLORS["bg_root"],
        ).pack(side="left", padx=(16, 0))

        scroll_wrap = Frame(self, bg=COLORS["bg_root"])
        scroll_wrap.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        judge_card = Card(scroll_wrap, padx=14, pady=14)
        judge_card.pack(fill="x", pady=(0, 8))
        Label(judge_card, text="판정 (AI)", font=FONT_HEAD, fg=COLORS["text_primary"], bg=COLORS["bg_card"]).pack(anchor="w")
        self._add_field(judge_card, "judge_backend", "백엔드", hint="unsup(비지도+YOLO) · yolo · both · stub · mock")
        self._add_field(judge_card, "judge_score_min", "YOLO NG 임계값", hint="confidence 0~1. 비지도는 면별 학습 문턱")
        self._add_field(judge_card, "judge_model_path", "YOLO 모델 경로", hint="비우면 crop640/weights/best.pt")
        self._add_field(judge_card, "history_max", "기록 보관 한도", hint="검사 기록 최대 건수")

        hw_card = Card(scroll_wrap, padx=14, pady=14)
        hw_card.pack(fill="x", pady=(0, 8))
        Label(hw_card, text="장치 상태", font=FONT_HEAD, fg=COLORS["text_primary"], bg=COLORS["bg_card"]).pack(anchor="w")
        self.hw_var = __import__("tkinter").StringVar(value="")
        Label(
            hw_card, textvariable=self.hw_var, font=FONT_BODY,
            fg=COLORS["text_secondary"], bg=COLORS["bg_card"], justify="left", anchor="w",
        ).pack(fill="x", pady=(8, 0))

        path_card = Card(scroll_wrap, padx=14, pady=14)
        path_card.pack(fill="x", pady=(0, 8))
        Label(path_card, text="경로 · 포트", font=FONT_HEAD, fg=COLORS["text_primary"], bg=COLORS["bg_card"]).pack(anchor="w")
        Label(
            path_card,
            text="샘플 면은 Geti OpenVINO. 크롭 확인: python crop_ui.py  ·  화각: python fov_ui.py  (화각 창은 운영 UI와 동시에 켜지 말 것)",
            font=FONT_SMALL, fg=COLORS["text_dim"], bg=COLORS["bg_card"],
        ).pack(anchor="w", pady=(6, 0))
        self.path_var = __import__("tkinter").StringVar(value="")
        Label(
            path_card, textvariable=self.path_var, font=FONT_BODY,
            fg=COLORS["text_secondary"], bg=COLORS["bg_card"], justify="left", anchor="w",
        ).pack(fill="x", pady=(8, 0))

        env_card = Card(scroll_wrap, padx=14, pady=14)
        env_card.pack(fill="x", pady=(0, 8))
        Label(env_card, text="실행기 환경 변수", font=FONT_HEAD, fg=COLORS["text_primary"], bg=COLORS["bg_card"]).pack(anchor="w")
        self.env_var = __import__("tkinter").StringVar(value="")
        Label(
            env_card, textvariable=self.env_var, font=("Consolas", 9),
            fg=COLORS["ok"], bg=COLORS["bg_cell"], justify="left", anchor="w",
            padx=10, pady=10,
        ).pack(fill="x", pady=(8, 0))

        self.status_var = __import__("tkinter").StringVar(value="")
        Label(self, textvariable=self.status_var, font=FONT_SMALL, fg=COLORS["text_dim"], bg=COLORS["bg_root"]).pack(anchor="w", padx=16)

        btn_row = Frame(self, bg=COLORS["bg_root"], padx=12)
        btn_row.pack(fill="x", pady=(0, 12))
        PillButton(btn_row, "↩ 검사 화면", self._go_back, variant="secondary").pack(side="left")
        PillButton(btn_row, "장치 새로고침", self.refresh_hardware, variant="secondary").pack(side="left", padx=(8, 0))
        PillButton(btn_row, "저장", self.save, variant="primary", padx=28).pack(side="right")

    def _add_field(self, parent, key, label, hint=""):
        row = Frame(parent, bg=COLORS["bg_card"])
        row.pack(fill="x", pady=(10, 0))
        Label(row, text=label, font=FONT_BODY, fg=COLORS["text_primary"], bg=COLORS["bg_card"], width=16, anchor="w").pack(side="left")
        var = StringVar()
        entry = Entry(
            row, textvariable=var, font=FONT_BODY, bg=COLORS["bg_cell"], fg=COLORS["text_primary"],
            insertbackground=COLORS["text_primary"], relief="flat", highlightthickness=1,
            highlightbackground=COLORS["border"], highlightcolor=COLORS["accent"],
        )
        entry.pack(side="left", fill="x", expand=True, ipady=4)
        self._fields[key] = var
        if hint:
            Label(row, text=hint, font=FONT_SMALL, fg=COLORS["text_dim"], bg=COLORS["bg_card"]).pack(side="left", padx=(8, 0))

    def _go_back(self):
        if self.on_back:
            self.on_back()

    def refresh_hardware(self):
        lines = []
        cams = connected_cameras()
        for cam in CAMERAS:
            device = cam.get("device") or "미연결"
            ok = any(c["id"] == cam["id"] for c in cams)
            mark = "● 연결" if ok else "○ 없음"
            lines.append(f"{cam['name']}  {device}  {mark}")
        self.hw_var.set("\n".join(lines))

    def refresh_paths(self, tcp_port=8585, capture_port=8586):
        self.path_var.set(
            f"검사 기록  {RECORDS_ROOT}\n"
            f"촬영 저장  {CAPTURE_DIR}\n"
            f"최근 판정  {RESULT_PATH}\n"
            f"TCP (실행기)  {tcp_port}\n"
            f"촬영 서버  {capture_port}"
        )

    def load_values(self):
        values = load_settings()
        for key, var in self._fields.items():
            var.set(values.get(key, ""))
            var.trace_add("write", lambda *_: self._update_env_preview())
        self.refresh_hardware()
        self.refresh_paths()
        self._update_env_preview()

    def _update_env_preview(self):
        values = {k: v.get() for k, v in self._fields.items()}
        self.env_var.set(settings_env_snippet(values))

    def save(self):
        values = {k: v.get().strip() for k, v in self._fields.items()}
        if values.get("judge_backend") not in self.BACKENDS:
            self.status_var.set("백엔드는 unsup, yolo, both, stub, mock, model 중 하나여야 합니다.")
            return
        try:
            score = float(values.get("judge_score_min", "0.5"))
            if not 0 <= score <= 1:
                raise ValueError
        except ValueError:
            self.status_var.set("YOLO NG 임계값은 0~1 사이 숫자여야 합니다.")
            return
        try:
            int(values.get("history_max", "200"))
        except ValueError:
            self.status_var.set("기록 보관 한도는 정수여야 합니다.")
            return
        save_settings(values)
        self._update_env_preview()
        self.status_var.set("저장됨 — robot_client 재시작 후 판정 설정이 적용됩니다.")


class PhaseHeader(Frame):
    """현재 단계 — 카드 안에 표시."""

    def __init__(self, parent):
        super().__init__(parent, bg=COLORS["bg_card"], pady=4)
        self.phase_var = __import__("tkinter").StringVar(value="대기")
        self.sub_var = __import__("tkinter").StringVar(value="연속 검사 버튼으로 시작")
        Label(self, text="현재 상태", font=FONT_SMALL, fg=COLORS["text_dim"], bg=COLORS["bg_card"]).pack(anchor="w")
        self.phase_label = Label(
            self, textvariable=self.phase_var, font=FONT_PHASE,
            fg=COLORS["text_primary"], bg=COLORS["bg_card"], anchor="w",
        )
        self.phase_label.pack(fill="x", pady=(2, 0))
        Label(self, textvariable=self.sub_var, font=FONT_SMALL, fg=COLORS["text_secondary"], bg=COLORS["bg_card"], anchor="w").pack(fill="x")

    def set_link(self, text, connected=False):
        pass

    def set_phase(self, text, sub="", running=False, error=False):
        self.phase_var.set(text)
        if sub:
            self.sub_var.set(sub)
        if running:
            fg, bg = COLORS["accent"], COLORS["bg_card"]
        elif error:
            fg, bg = COLORS["ng"], COLORS["bg_card"]
        else:
            fg, bg = COLORS["text_primary"], COLORS["bg_card"]
        self.phase_label.config(fg=fg, bg=bg)
        self.config(bg=bg)


class TimelineStrip(Frame):
    CHIP_W, CHIP_H = 74, 46

    def __init__(self, parent):
        super().__init__(parent, bg=COLORS["bg_card"], pady=4)
        Label(self, text="공정 진행", font=FONT_SMALL, fg=COLORS["text_dim"], bg=COLORS["bg_card"]).pack(anchor="w", padx=2)
        self.cells = {}
        self._chips = {
            state: rounded_image(self.CHIP_W, self.CHIP_H, 14, COLORS[fill], COLORS["bg_card"])
            for state, fill in (("idle", "bg_cell"), ("done", "ok_glow"), ("current", "accent"))
        }
        row = Frame(self, bg=COLORS["bg_card"])
        row.pack(fill="x", pady=(6, 0))
        for num, cmd, short in TIMELINE_STEPS:
            cell = Label(
                row, text=f"{num}\n{short}", font=("Segoe UI", 8), compound="center",
                image=self._chips["idle"], bg=COLORS["bg_card"], fg=COLORS["text_dim"],
                relief="flat", borderwidth=0, highlightthickness=0,
            )
            cell.pack(side="left", padx=3)
            self.cells[cmd] = cell
        self._current = None

    def _set_state(self, cell, state, fg):
        cell.config(image=self._chips[state], fg=fg)

    def reset(self):
        self._current = None
        for cell in self.cells.values():
            self._set_state(cell, "idle", COLORS["text_dim"])

    def mark_done(self, command):
        cell = self.cells.get(command)
        if cell:
            self._set_state(cell, "done", "#DCFCE7")

    def mark_current(self, command):
        if self._current and self._current != command:
            self.mark_done(self._current)
        self._current = command
        cell = self.cells.get(command)
        if cell:
            self._set_state(cell, "current", COLORS["bg_root"])


class OperatorPanel(Card):
    """Vision Mate 스타일 — 판정 · 통계 · 시작/중지/비상정지."""

    def __init__(self, parent, on_run, on_stop, on_estop=None):
        super().__init__(parent, padx=14, pady=14)
        self.pack(side="right", fill="y", padx=(0, 12), pady=12)
        self.configure(width=300)
        self.pack_propagate(False)
        self.on_run = on_run
        self.on_stop = on_stop
        self.on_estop = on_estop or on_stop

        Label(self, text="검사 결과", font=FONT_HEAD, fg=COLORS["text_primary"], bg=COLORS["bg_card"]).pack(anchor="w")

        self.verdict_frame = Frame(self, bg=COLORS["bg_cell"], pady=16)
        self.verdict_frame.pack(fill="x", pady=(10, 8))
        self.verdict_var = __import__("tkinter").StringVar(value="—")
        self.verdict_label = Label(
            self.verdict_frame, textvariable=self.verdict_var, font=FONT_HERO,
            fg=COLORS["text_dim"], bg=COLORS["bg_cell"],
        )
        self.verdict_label.pack()
        self.verdict_sub = __import__("tkinter").StringVar(value="판정 대기")
        Label(
            self.verdict_frame, textvariable=self.verdict_sub, font=FONT_SMALL,
            fg=COLORS["text_secondary"], bg=COLORS["bg_cell"],
        ).pack(pady=(4, 0))

        stats = Frame(self, bg=COLORS["bg_card"])
        stats.pack(fill="x", pady=(4, 12))
        self.stat_total = __import__("tkinter").StringVar(value="0")
        self.stat_ok = __import__("tkinter").StringVar(value="0")
        self.stat_rate = __import__("tkinter").StringVar(value="—")
        for title, var in (("Total", self.stat_total), ("OK", self.stat_ok), ("OK Rate", self.stat_rate)):
            box = Frame(stats, bg=COLORS["bg_cell"], padx=12, pady=10)
            box.pack(side="left", fill="x", expand=True, padx=2)
            Label(box, text=title, font=FONT_SMALL, fg=COLORS["text_dim"], bg=COLORS["bg_cell"]).pack(anchor="w")
            Label(box, textvariable=var, font=FONT_STAT, fg=COLORS["text_primary"], bg=COLORS["bg_cell"]).pack(anchor="w")

        btn_row = Frame(self, bg=COLORS["bg_card"])
        btn_row.pack(fill="x", pady=(0, 12))
        action_w = 272
        self.run_btn = PillButton(
            btn_row, "▶  검사 시작", self.on_run, variant="primary",
            font=FONT_HEAD, width=action_w, pady=14,
            surface=COLORS["bg_card"],
        )
        self.run_btn.pack(pady=(0, 8))
        self.stop_btn = PillButton(
            btn_row, "■  검사 중지", self.on_stop, variant="stop_on",
            font=FONT_HEAD, width=action_w, pady=14,
            surface=COLORS["bg_card"],
        )
        self.stop_btn.pack(pady=(0, 8))
        self.estop_btn = PillButton(
            btn_row, "비상정지", self.on_estop, variant="secondary",
            font=FONT_HEAD, width=action_w, pady=14,
            surface=COLORS["bg_card"],
        )
        self.estop_btn.pack()

        Label(self, text="비지도", font=FONT_HEAD, fg=COLORS["text_primary"], bg=COLORS["bg_card"]).pack(anchor="w", pady=(8, 2))
        self.unsup_summary = __import__("tkinter").StringVar(value="판정 후 표시")
        Label(self, textvariable=self.unsup_summary, font=FONT_SMALL, fg=COLORS["text_dim"], bg=COLORS["bg_card"], wraplength=260, justify="left").pack(anchor="w")
        self.unsup_list = self._score_listbox(self, pady=(4, 8))

        Label(self, text="YOLO", font=FONT_HEAD, fg=COLORS["text_primary"], bg=COLORS["bg_card"]).pack(anchor="w", pady=(0, 2))
        self.yolo_summary = __import__("tkinter").StringVar(value="스크래치 · 찌그러짐")
        Label(self, textvariable=self.yolo_summary, font=FONT_SMALL, fg=COLORS["text_dim"], bg=COLORS["bg_card"], wraplength=260, justify="left").pack(anchor="w")
        self.yolo_list = self._score_listbox(self, pady=(4, 0))

        self.inspect_wall = None
        self.list_items = []
        self.unsup_items = []
        self.yolo_items = []
        self.listbox = self.unsup_list
        self.defect_summary = self.unsup_summary

    def _score_listbox(self, parent, pady=(4, 0)):
        wrap = Frame(parent, bg=COLORS["bg_cell"], highlightthickness=0)
        wrap.pack(fill="both", expand=True, pady=pady)
        box = Listbox(
            wrap, font=FONT_BODY, height=5,
            bg=COLORS["bg_root"], fg=COLORS["text_primary"],
            selectbackground=COLORS["accent_soft"], selectforeground=COLORS["accent"],
            relief="flat", borderwidth=0, highlightthickness=0, activestyle="none",
        )
        scroll = ttk.Scrollbar(
            wrap, orient="vertical", command=box.yview, style="Dark.Vertical.TScrollbar",
        )
        box.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        scroll.pack(side="right", fill="y", padx=(0, 4), pady=4)
        box.config(yscrollcommand=scroll.set)
        return box

    def set_controls(self, idle=True):
        self.run_btn.set_enabled(idle)

    def set_stats(self, total, ok):
        rate = f"{ok / total * 100:.1f}%" if total else "—"
        self.stat_total.set(str(total))
        self.stat_ok.set(str(ok))
        self.stat_rate.set(rate)

    def show_idle_verdict(self):
        self.verdict_var.set("—")
        self.verdict_sub.set("판정 대기")
        self.verdict_frame.config(bg=COLORS["bg_cell"])
        self.verdict_label.config(bg=COLORS["bg_cell"], fg=COLORS["text_dim"])

    def set_verdict(self, verdict, judgment=None):
        judgment = judgment or {}
        unsup_items, yolo_items = split_judge_items(judgment)
        unsup_ng = sum(1 for item in unsup_items if item.get("ng"))
        self.verdict_var.set(verdict)
        if verdict == "OK":
            bg, fg = COLORS["ok_bg"], COLORS["ok"]
            if judgment.get("backend") in LIVE_BACKENDS:
                self.verdict_sub.set("비지도 · YOLO 통과")
            else:
                self.verdict_sub.set("불량 없음")
        elif verdict == "NG":
            bg, fg = COLORS["ng_bg"], COLORS["ng"]
            parts = []
            if unsup_items:
                parts.append(f"비지도 {unsup_ng}면")
            if yolo_items:
                parts.append(f"YOLO {yolo_category_summary(yolo_items)}")
            elif judgment.get("backend") in LIVE_BACKENDS:
                parts.append("YOLO 검출 없음")
            self.verdict_sub.set(" · ".join(parts) or f"불량 {len(judgment.get('defects') or [])}건")
        else:
            bg, fg = COLORS["bg_cell"], COLORS["text_dim"]
            self.verdict_sub.set("")
        self.verdict_frame.config(bg=bg)
        self.verdict_label.config(bg=bg, fg=fg)

    def clear_defects(self):
        self.unsup_list.delete(0, END)
        self.yolo_list.delete(0, END)
        self.list_items = []
        self.unsup_items = []
        self.yolo_items = []
        self.unsup_summary.set("판정 후 표시")
        self.yolo_summary.set("스크래치 · 찌그러짐")
        if self.inspect_wall:
            self.inspect_wall.highlight_defect(-1)

    def set_judgment(self, judgment, inspect_wall=None):
        if inspect_wall is not None:
            self.inspect_wall = inspect_wall
        unsup_items, yolo_items = split_judge_items(judgment)
        self.unsup_items = unsup_items
        self.yolo_items = yolo_items
        self.list_items = unsup_items + yolo_items
        self.unsup_list.delete(0, END)
        self.yolo_list.delete(0, END)
        if unsup_items:
            ng_n = sum(1 for item in unsup_items if item.get("ng"))
            self.unsup_summary.set(f"{len(unsup_items)}면 · 이상 {ng_n}면 · 선택 시 위치 강조")
            for item in unsup_items:
                self.unsup_list.insert(END, defect_line(item))
        else:
            self.unsup_summary.set("이번 판정 없음")
        if yolo_items:
            self.yolo_summary.set(yolo_category_summary(yolo_items) + " · 선택 시 위치 강조")
            for item in yolo_items:
                self.yolo_list.insert(END, defect_line(item))
        else:
            self.yolo_summary.set("검출 없음  ·  스크래치 0 · 찌그러짐 0")
        if not unsup_items and not yolo_items and self.inspect_wall:
            self.inspect_wall.highlight_defect(-1)

    def bind_defect_select(self, callback):
        def on_unsup(event):
            self.yolo_list.selection_clear(0, END)
            callback(event)

        def on_yolo(event):
            self.unsup_list.selection_clear(0, END)
            callback(event)

        self.unsup_list.bind("<<ListboxSelect>>", on_unsup)
        self.yolo_list.bind("<<ListboxSelect>>", on_yolo)


class VerdictBar(Card):
    """remote_test_ui 호환 — 간단 판정 표시."""

    def __init__(self, parent):
        super().__init__(parent, padx=12, pady=10)
        self.verdict_var = __import__("tkinter").StringVar(value="")
        self.detail_var = __import__("tkinter").StringVar(value="")
        Label(self, textvariable=self.verdict_var, font=FONT_HERO, fg=COLORS["text_dim"], bg=COLORS["bg_card"]).pack(side="left")
        Label(self, textvariable=self.detail_var, font=FONT_BODY, fg=COLORS["text_secondary"], bg=COLORS["bg_card"]).pack(side="left", padx=12)

    def show_idle(self):
        self.pack_forget()

    def set_verdict(self, verdict, judgment=None):
        judgment = judgment or {}
        if not self.winfo_ismapped():
            self.pack(fill="x", padx=8, pady=8)
        self.verdict_var.set(verdict)
        defects = judgment.get("defects") or []
        if verdict == "OK":
            self.detail_var.set("불량 없음")
        elif verdict == "NG":
            self.detail_var.set(f"불량 {len(defects)}건")
        else:
            self.detail_var.set("")


class InspectWall(Frame):
    def __init__(self, parent, cell_size=CELL_SIZE, auto_pack=True, show_header=True):
        super().__init__(parent, bg=COLORS["bg_root"])
        self.cell_size = cell_size
        if auto_pack:
            self.pack(fill="both", expand=True)
        self.cells = {}
        self.photos = {}
        self.judgment = {}
        self.manifests = {}

        outer = Card(self, padx=10, pady=10)
        outer.pack(fill="both", expand=True, padx=8, pady=8)
        if show_header:
            Label(outer, text="검사 영상", font=FONT_HEAD, fg=COLORS["text_primary"], bg=COLORS["bg_card"]).pack(anchor="w")
            self.subtitle = Label(
                outer,
                text="1차 4면 · 2차 2면 (임시 분리)",
                font=FONT_SMALL, fg=COLORS["text_dim"], bg=COLORS["bg_card"],
            )
            self.subtitle.pack(anchor="w", pady=(0, 8))
        else:
            self.subtitle = None

        sections = Frame(outer, bg=COLORS["bg_card"])
        sections.pack(fill="both", expand=True)
        section_meta = (
            ("1차", "1차 검사", "카메라 1~4"),
            ("2차", "2차 검사", "카메라 3·4 · 서보 180°"),
        )
        for inspect, title, hint in section_meta:
            block = Frame(sections, bg=COLORS["bg_card"])
            block.pack(fill="both", expand=True, pady=(0, 8))
            head = Frame(block, bg=COLORS["bg_card"])
            head.pack(fill="x", pady=(0, 4))
            Label(head, text=title, font=FONT_HEAD, fg=COLORS["text_primary"], bg=COLORS["bg_card"]).pack(side="left")
            Label(head, text=hint, font=FONT_SMALL, fg=COLORS["text_dim"], bg=COLORS["bg_card"]).pack(side="left", padx=(10, 0))
            grid = Frame(block, bg=COLORS["bg_card"])
            grid.pack(fill="both", expand=True)
            faces = INSPECT_FACES[inspect]
            cols = max(len(faces), 1)
            for col in range(cols):
                grid.columnconfigure(col, weight=1)
            grid.rowconfigure(0, weight=1)
            for col, face in enumerate(faces):
                face_id = int(face["id"])
                wrap = Frame(grid, bg=COLORS["bg_cell"], highlightthickness=0)
                wrap.grid(row=0, column=col, padx=5, pady=5, sticky="nsew")
                Label(
                    wrap, text=face["name"], font=FONT_SMALL,
                    fg=COLORS["text_dim"], bg=COLORS["bg_cell"],
                ).pack(anchor="w", padx=8, pady=(6, 0))
                cell = Label(wrap, text="—", bg=COLORS["bg_cell"], fg=COLORS["text_dim"], relief="flat")
                cell.pack(fill="both", expand=True, padx=4, pady=4)
                self.cells[(inspect, face_id)] = cell

    def clear(self, inspect=None):
        if inspect:
            self.manifests.pop(inspect, None)
            for key, cell in self.cells.items():
                if key[0] == inspect:
                    cell.config(image="", text="—")
                    self.photos.pop(key, None)
            return
        self.manifests.clear()
        self.judgment = {}
        for cell in self.cells.values():
            cell.config(image="", text="—")

    def set_judgment(self, judgment):
        self.judgment = judgment or {}
        self._refresh_all_cells()

    def load_capture_folders(self, captures):
        for label, folder in (captures or {}).items():
            if not folder:
                continue
            manifest = load_manifest(folder)
            if manifest:
                self.manifests[label] = manifest
        self._refresh_all_cells()

    def _defects_for(self, inspect, cam_id):
        return [
            item.get("bbox")
            for item in self.judgment.get("defects") or []
            if item.get("inspect") == inspect and int(item.get("cam_id", 0)) == cam_id
        ]

    def _refresh_all_cells(self, highlight=None):
        for (inspect, cam_id), cell in self.cells.items():
            manifest = self.manifests.get(inspect)
            path = None
            if manifest:
                for cam in manifest.get("cameras") or []:
                    if int(cam.get("id", 0)) == cam_id:
                        path = image_path_from_cam(cam)
                        break
            bboxes = self._defects_for(inspect, cam_id)
            highlight_idx = -1
            if highlight:
                h_inspect, h_cam, h_idx = highlight
                if h_inspect == inspect and h_cam == cam_id:
                    highlight_idx = h_idx
            key = (inspect, cam_id)
            if path and Path(path).is_file():
                try:
                    photo = render_image(
                        path, bboxes, highlight_idx, self.cell_size, cam_id=cam_id, inspect=inspect,
                    )
                    self.photos[key] = photo
                    cell.config(image=photo, text="", compound="center")
                except OSError:
                    cell.config(image="", text="—")
            else:
                cell.config(image="", text="—")

    def highlight_defect(self, defect_index):
        defects = self.judgment.get("defects") or []
        if defect_index < 0 or defect_index >= len(defects):
            self._refresh_all_cells()
            return
        self.highlight_item(defects[defect_index])

    def highlight_item(self, item):
        if not item:
            self._refresh_all_cells()
            return
        cam_id = int(item.get("cam_id", 0))
        inspect = item.get("inspect", "")
        target = _bbox_key(item.get("bbox"))
        defects = self.judgment.get("defects") or []
        local_idx = -1
        fallback = -1
        count = 0
        for defect in defects:
            if defect.get("inspect") != inspect or int(defect.get("cam_id", 0)) != cam_id:
                continue
            if fallback < 0:
                fallback = count
            if target and _bbox_key(defect.get("bbox")) == target:
                local_idx = count
                break
            count += 1
        if local_idx < 0:
            local_idx = fallback
        if local_idx < 0:
            self._refresh_all_cells()
            return
        self._refresh_all_cells(highlight=(inspect, cam_id, local_idx))


class DefectPanel(Frame):
    """remote_test_ui 호환 — 라이트 테마."""

    def __init__(self, parent, width=260):
        super().__init__(parent, width=width, bg="#f8fafc")
        self.pack(side="right", fill="y")
        self.pack_propagate(False)
        self.inspect_wall = None
        self.summary = __import__("tkinter").StringVar(value="판정 후 표시")
        Label(self, text="불량 위치", font=FONT_HEAD, bg="#f8fafc").pack(anchor="w", padx=10, pady=8)
        list_frame = Frame(self)
        list_frame.pack(fill="both", expand=True, padx=8)
        scroll = Scrollbar(list_frame)
        scroll.pack(side="right", fill="y")
        self.listbox = Listbox(list_frame, yscrollcommand=scroll.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        scroll.config(command=self.listbox.yview)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

    def clear(self):
        self.listbox.delete(0, END)

    def set_judgment(self, judgment, inspect_wall=None):
        if inspect_wall is not None:
            self.inspect_wall = inspect_wall
        self.listbox.delete(0, END)
        for item in (judgment or {}).get("defects") or []:
            self.listbox.insert(END, str(item.get("class_name")))

    def _on_select(self, _event):
        sel = self.listbox.curselection()
        if sel and self.inspect_wall:
            self.inspect_wall.highlight_defect(int(sel[0]))


class RemoteServer:
    def __init__(self, host="0.0.0.0", port=8585):
        self.host = host
        self.port = port
        self.conn = None
        self.conn_file = None
        self.on_connect = None
        self.on_disconnect = None

    def start_background(self):
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((self.host, self.port))
            except OSError as exc:
                print(f"[GUI] 포트 {self.port} 바인드 실패 — {exc}")
                return
            s.listen()
            print(f"[GUI] 포트 {self.port} 대기")
            while True:
                c, addr = s.accept()
                if self.conn is not None:
                    try:
                        self.conn.close()
                    except Exception:
                        pass
                self.conn = c
                self.conn_file = c.makefile("r")
                if self.on_connect:
                    self.on_connect(addr)

    def send(self, cmd):
        if self.conn is None:
            return None
        msg = {"command": cmd, "timestamp": time.time()}
        self.conn.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        line = self.conn_file.readline()
        if not line:
            if self.on_disconnect:
                self.on_disconnect()
            return None
        return json.loads(line)
