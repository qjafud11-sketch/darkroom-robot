"""수집·판정 크롭이 샘플만 남겼는지 확인하는 창.

수집·판정 모두 같은 crop_camera 를 쓴다. 왼쪽이 테두리, 오른쪽이
모델에 들어가는 조각이다. 이미 자른 수집본은 저장본만 보여 준다.

  source .venv/bin/activate
  python crop_ui.py

카메라는 쓰지 않는다. 운영 UI와 같이 켜도 된다. 세그는 .venv 가 필요하다.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, LEFT, RIGHT, Frame, Label, Listbox, Scrollbar, StringVar, Tk
import tkinter as tk

from PIL import Image, ImageDraw, ImageFilter, ImageTk

from camera import CAPTURE_DIR
from dataset_label import FACES, OKSET_DIR, list_samples, shot_path
from sample_roi import SampleNotFound, preview_crop

PAIR_MAX_SEC = 15 * 60
ORIG_W, ORIG_H = 260, 146
CROP_W, CROP_H = 200, 146
CYAN = (34, 211, 238)


@dataclass
class SetItem:
    kind: str
    title: str
    first: Path | None = None
    second: Path | None = None
    sample: Path | None = None
    stamp: datetime = field(default_factory=lambda: datetime.min)

    def path_for(self, face: dict) -> Path | None:
        if self.kind == "collect" and self.sample is not None:
            path = shot_path(self.sample, face["stage"], face["cam"])
            return path if path.is_file() else None
        folder = self.first if face["stage"] == 1 else self.second
        if folder is None:
            return None
        path = folder / f"cam{face['cam']}.jpg"
        return path if path.is_file() else None


class FaceCard:
    def __init__(self, parent, face: dict, on_open):
        self.face = face
        self.on_open = on_open
        self.info = None
        self.orig_photo = None
        self.crop_photo = None
        self.box = Frame(parent, bd=1, relief="solid", cursor="hand2")
        self.box.pack(side="left", padx=4, pady=4, fill="both", expand=True)
        stage = "1차" if face["stage"] == 1 else "2차"
        self.title = Label(
            self.box,
            text=f"{face['name']}  ·  카메라 {face['cam']}  ·  {stage}",
            font=("Arial", 10, "bold"),
        )
        self.title.pack(pady=(6, 0))
        pics = Frame(self.box)
        pics.pack(padx=6, pady=4)
        left = Frame(pics)
        left.pack(side="left", padx=(0, 4))
        Label(left, text="원본 + 테두리", fg="#555", font=("Arial", 8)).pack()
        self.orig = Label(left, text="대기", bg="#111", fg="#ddd", width=40, height=10)
        self.orig.pack()
        right = Frame(pics)
        right.pack(side="left")
        Label(right, text="크롭 결과", fg="#555", font=("Arial", 8)).pack()
        self.crop = Label(right, text="대기", bg="#111", fg="#ddd", width=30, height=10)
        self.crop.pack()
        self.meta = Label(self.box, text="—", fg="#333", wraplength=560, justify="left")
        self.meta.pack(anchor="w", padx=8, pady=(0, 8))
        _bind_click(self.box, self._open)

    def _open(self, _event=None):
        if self.info is not None:
            self.on_open(self.face, self.info)

    def clear(self, text="대기"):
        self.info = None
        self.orig_photo = None
        self.crop_photo = None
        self.orig.config(image="", text=text, bg="#111", fg="#ddd")
        self.crop.config(image="", text=text, bg="#111", fg="#ddd")
        self.meta.config(text="—")

    def show(self, info: dict | None, error: str | None = None):
        if error:
            self.clear(error)
            self.meta.config(text=error)
            return
        if info is None:
            self.clear("파일 없음")
            self.meta.config(text="이 면 파일이 없습니다.")
            return
        self.info = info
        overlay = _draw_overlay(info["rgb"], info["result"]) if not info["already"] else info["rgb"]
        self.orig_photo = _photo(overlay, ORIG_W, ORIG_H)
        self.crop_photo = _photo(info["result"].image, CROP_W, CROP_H)
        self.orig.config(image=self.orig_photo, text="")
        self.crop.config(image=self.crop_photo, text="")
        self.meta.config(text=_meta_text(info))


class CropUi:
    def __init__(self):
        self.root = Tk()
        self.root.title("크롭 확인")
        self.root.geometry("1680x960")
        self.root.minsize(1400, 820)
        self.mode = StringVar(value="collect")
        self.status = StringVar(value="왼쪽에서 세트를 고르면 6면 크롭을 봅니다.")
        self.items: list[SetItem] = []
        self.selected: SetItem | None = None
        self.busy = False
        self._gen = 0
        self.cards: dict[str, FaceCard] = {}
        self._detail = None
        self._detail_photos = []
        self._build()
        self._reload_list()
        threading.Thread(target=self._warmup, daemon=True).start()

    def _build(self):
        top = Frame(self.root)
        top.pack(fill="x", padx=10, pady=(10, 4))
        Label(top, text="크롭 확인", font=("Arial", 14, "bold")).pack(side="left")
        Label(
            top,
            text="면을 클릭하면 크게 봅니다. 수집이 아직 원본이면 저장될 크롭을 미리 보여 줍니다.",
            fg="#555",
        ).pack(side="left", padx=12)

        modes = Frame(self.root)
        modes.pack(fill="x", padx=10, pady=6)
        self.collect_btn = tk.Button(
            modes,
            text="데이터 수집",
            width=16,
            height=2,
            command=lambda: self._set_mode("collect"),
        )
        self.collect_btn.pack(side="left", padx=4)
        self.inspect_btn = tk.Button(
            modes,
            text="판정 촬영",
            width=16,
            height=2,
            command=lambda: self._set_mode("inspect"),
        )
        self.inspect_btn.pack(side="left", padx=4)
        tk.Button(modes, text="목록 새로고침", width=12, height=2, command=self._reload_list).pack(
            side="left", padx=8
        )
        tk.Button(modes, text="다시 크롭", width=12, height=2, command=self._reload_selected).pack(
            side="left", padx=4
        )

        body = Frame(self.root)
        body.pack(fill="both", expand=True, padx=10, pady=4)

        side = Frame(body, width=300)
        side.pack(side="left", fill="y", padx=(0, 8))
        side.pack_propagate(False)
        Label(side, text="세트", font=("Arial", 10, "bold")).pack(anchor="w", pady=(0, 4))
        scroll = Scrollbar(side)
        scroll.pack(side=RIGHT, fill="y")
        self.listbox = Listbox(side, yscrollcommand=scroll.set, font=("Arial", 11), height=28)
        self.listbox.pack(side=LEFT, fill=BOTH, expand=True)
        scroll.config(command=self.listbox.yview)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        self.root.bind("<Up>", lambda e: self._nudge(-1))
        self.root.bind("<Down>", lambda e: self._nudge(1))

        grid = Frame(body)
        grid.pack(side="left", fill="both", expand=True)
        row1 = Frame(grid)
        row1.pack(fill="both", expand=True)
        row2 = Frame(grid)
        row2.pack(fill="both", expand=True)
        for index, face in enumerate(FACES):
            parent = row1 if index < 3 else row2
            self.cards[face["key"]] = FaceCard(parent, face, self._open_detail)

        Label(self.root, textvariable=self.status, wraplength=1600, justify="left").pack(
            fill="x", padx=10, pady=8, anchor="w"
        )
        self._paint_mode()

    def _set_mode(self, mode: str):
        if self.mode.get() == mode and self.items:
            return
        self.mode.set(mode)
        self._paint_mode()
        self._reload_list()

    def _paint_mode(self):
        on = "#fde68a"
        off = self.root.cget("bg")
        self.collect_btn.config(relief="sunken" if self.mode.get() == "collect" else "raised", bg=on if self.mode.get() == "collect" else off)
        self.inspect_btn.config(relief="sunken" if self.mode.get() == "inspect" else "raised", bg=on if self.mode.get() == "inspect" else off)

    def _reload_list(self):
        if self.mode.get() == "collect":
            self.items = _collect_items()
            self.status.set(f"수집 세트 {len(self.items)}개. cropset·okset·dataset 입니다.")
        else:
            self.items = _inspect_items()
            self.status.set(f"판정 촬영 {len(self.items)}세트. 1차·2차를 시간으로 짝지었습니다.")
        self.listbox.delete(0, "end")
        for item in self.items:
            self.listbox.insert("end", item.title)
        self.selected = None
        for card in self.cards.values():
            card.clear()
        if self.items:
            self.listbox.selection_set(0)
            self._load_item(self.items[0])

    def _on_select(self, _event=None):
        indexes = self.listbox.curselection()
        if not indexes:
            return
        self._load_item(self.items[indexes[0]])

    def _nudge(self, step: int):
        if not self.items:
            return
        indexes = self.listbox.curselection()
        index = indexes[0] if indexes else 0
        index = max(0, min(len(self.items) - 1, index + step))
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(index)
        self.listbox.see(index)
        self._load_item(self.items[index])

    def _reload_selected(self):
        if self.selected is not None:
            self._load_item(self.selected)

    def _load_item(self, item: SetItem):
        self.selected = item
        self._gen += 1
        gen = self._gen
        self.busy = True
        self.status.set(f"{item.title}  크롭 확인 중…")
        for card in self.cards.values():
            card.clear("확인 중")

        def work():
            rows = []
            for face in FACES:
                path = item.path_for(face)
                if path is None:
                    rows.append((face["key"], None, None))
                    continue
                try:
                    rows.append((face["key"], preview_crop(path, face["cam"], stage=face["stage"]), None))
                except SampleNotFound as exc:
                    rows.append((face["key"], None, str(exc)))
                except Exception as exc:
                    rows.append((face["key"], None, str(exc)))
            self.root.after(0, lambda: self._apply(gen, item, rows))

        threading.Thread(target=work, daemon=True).start()

    def _apply(self, gen: int, item: SetItem, rows: list):
        if gen != self._gen:
            return
        self.busy = False
        ok = 0
        saved = 0
        for key, info, error in rows:
            self.cards[key].show(info, error=error)
            if info is not None:
                ok += 1
                if info["already"]:
                    saved += 1
        sources = []
        for _key, info, _error in rows:
            if info and info.get("source"):
                sources.append(info["source"].split(" · ")[0])
        src = ",".join(sorted(set(sources))) if sources else "—"
        if item.kind == "collect" and saved:
            kind = f"수집 저장본 {saved}"
        elif item.kind == "collect":
            kind = "수집 원본 → 저장될 크롭"
        else:
            kind = "판정 크롭"
        self.status.set(f"{item.title}  ·  {kind}  ·  {ok}/{len(FACES)}면  ·  {src}")

    def _open_detail(self, face, info):
        if self._detail is None or not self._detail.winfo_exists():
            win = tk.Toplevel(self.root)
            win.title("크롭 자세히")
            win.geometry("1520x820")
            self._detail_title = Label(win, font=("Arial", 13, "bold"))
            self._detail_title.pack(pady=(10, 4))
            self._detail_meta = Label(win, fg="#333", wraplength=1400, justify="left")
            self._detail_meta.pack()
            pics = Frame(win)
            pics.pack(fill="both", expand=True, padx=10, pady=8)
            left = Frame(pics)
            left.pack(side="left", fill="both", expand=True)
            Label(left, text="원본 + 테두리", font=("Arial", 10, "bold")).pack()
            self._detail_orig = Label(left, bg="#111", fg="#ddd")
            self._detail_orig.pack(padx=6, pady=6)
            right = Frame(pics)
            right.pack(side="left", fill="both", expand=True)
            Label(right, text="크롭 결과", font=("Arial", 10, "bold")).pack()
            self._detail_crop = Label(right, bg="#111", fg="#ddd")
            self._detail_crop.pack(padx=6, pady=6)
            self._detail = win
        overlay = _draw_overlay(info["rgb"], info["result"]) if not info["already"] else info["rgb"]
        self._detail_photos = [
            _photo(overlay, 900, 506),
            _photo(info["result"].image, 540, 506),
        ]
        self._detail_orig.config(image=self._detail_photos[0], text="")
        self._detail_crop.config(image=self._detail_photos[1], text="")
        stage = "1차" if face["stage"] == 1 else "2차"
        self._detail_title.config(text=f"{face['name']}  ·  카메라 {face['cam']}  ·  {stage}")
        self._detail_meta.config(text=_meta_text(info))
        self._detail.deiconify()
        self._detail.lift()

    def _warmup(self):
        try:
            from sample_ov import available as ov_ok
            from sample_ov import warmup_safe, weights_path
            from sample_seg import available as yolo_ok

            if ov_ok() and warmup_safe():
                self.root.after(0, lambda: self.status.set(f"Geti 샘플면 준비됨  {weights_path()}"))
                return
            if not yolo_ok():
                self.root.after(0, lambda: self.status.set("Geti·세그 모델이 없어 샘플 면을 못 찾습니다."))
                return
            self.root.after(0, lambda: self.status.set("Geti 없음. YOLO 세그로 샘플 면을 찾습니다."))
        except Exception as exc:
            self.root.after(0, lambda: self.status.set(f"샘플 면 준비 실패 — {exc}"))

    def run(self):
        self.root.mainloop()


def _fit(image: Image.Image, tw: int, th: int) -> Image.Image:
    rgb = image.convert("RGB")
    rgb.thumbnail((tw, th), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (tw, th), (17, 17, 17))
    canvas.paste(rgb, ((tw - rgb.width) // 2, (th - rgb.height) // 2))
    return canvas


def _photo(image: Image.Image, tw: int, th: int) -> ImageTk.PhotoImage:
    return ImageTk.PhotoImage(_fit(image, tw, th))


def _bind_click(widget, fn):
    widget.bind("<Button-1>", fn)
    for child in widget.winfo_children():
        _bind_click(child, fn)


def _meta_text(info: dict) -> str:
    w, h = info["size"]
    src = info["source"] or "—"
    tilt = "기울기 보정" if info["tilted"] else "축정렬"
    saved = "수집 저장본" if info["already"] else "저장·판정에 쓰는 크롭"
    return f"{info['path'].name}  ·  {w}×{h}  ·  {src}  ·  {tilt}  ·  {saved}"


def _draw_overlay(rgb: Image.Image, result) -> Image.Image:
    vis = rgb.convert("RGB").copy()
    draw = ImageDraw.Draw(vis)
    mask = getattr(result, "mask", None)
    if mask is not None and getattr(mask, "any", lambda: False)():
        try:
            import numpy as np

            edge = Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.FIND_EDGES)
            pix = np.asarray(vis)
            on = np.asarray(edge) > 0
            pix[on] = CYAN
            vis = Image.fromarray(pix)
            draw = ImageDraw.Draw(vis)
        except Exception:
            pass
    if result.corners and len(result.corners) == 4:
        poly = [(int(x), int(y)) for x, y in result.corners]
        draw.polygon(poly, outline=CYAN, width=4)
    else:
        draw.rectangle(result.box, outline=CYAN, width=4)
    return vis


def _parse_stamp(name: str) -> datetime | None:
    parts = name.split("_")
    if len(parts) < 3:
        return None
    try:
        return datetime.strptime(f"{parts[1]}_{parts[2]}", "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _collect_items() -> list[SetItem]:
    folders = list_samples()
    folders.sort(key=lambda p: (0 if p.parent == OKSET_DIR else 1, -p.stat().st_mtime))
    items = []
    for folder in folders:
        stamp = datetime.fromtimestamp(folder.stat().st_mtime)
        if folder.parent == OKSET_DIR:
            where = "okset"
        elif folder.parent.name == "cropset" or folder.name.startswith("crop_"):
            where = "cropset"
        else:
            where = folder.parent.name
        items.append(
            SetItem(
                kind="collect",
                title=f"{folder.name}  ·  {where}",
                sample=folder,
                stamp=stamp,
            )
        )
    return items


def _inspect_items() -> list[SetItem]:
    firsts = sorted(CAPTURE_DIR.glob("1차_*"), key=lambda p: p.name, reverse=True)
    seconds = [p for p in CAPTURE_DIR.glob("2차_*") if p.is_dir()]
    used: set[Path] = set()
    items: list[SetItem] = []
    for first in firsts:
        if not first.is_dir():
            continue
        t1 = _parse_stamp(first.name)
        peer = None
        peer_dt = None
        if t1 is not None:
            for second in seconds:
                if second in used:
                    continue
                t2 = _parse_stamp(second.name)
                if t2 is None:
                    continue
                delta = (t2 - t1).total_seconds()
                if 0 <= delta <= PAIR_MAX_SEC and (peer_dt is None or t2 < peer_dt):
                    peer, peer_dt = second, t2
        if peer is not None:
            used.add(peer)
        stamp = t1 or datetime.fromtimestamp(first.stat().st_mtime)
        pair = "1차+2차" if peer is not None else "1차만"
        items.append(
            SetItem(
                kind="inspect",
                title=f"{stamp.strftime('%m-%d %H:%M:%S')}  ·  {pair}  ·  {first.name}",
                first=first,
                second=peer,
                stamp=stamp,
            )
        )
    leftovers = [p for p in seconds if p not in used]
    leftovers.sort(key=lambda p: p.name, reverse=True)
    for second in leftovers:
        t2 = _parse_stamp(second.name) or datetime.fromtimestamp(second.stat().st_mtime)
        items.append(
            SetItem(
                kind="inspect",
                title=f"{t2.strftime('%m-%d %H:%M:%S')}  ·  2차만  ·  {second.name}",
                second=second,
                stamp=t2,
            )
        )
    items.sort(key=lambda item: item.stamp, reverse=True)
    return items


def main():
    CropUi().run()


if __name__ == "__main__":
    main()
