"""데이터 수집 — 사람이 시료를 넣고 빼면, 버튼 한 번에 6장을 찍는다.

파이프라인 2~5번만 자동이다.
  2  1차 검사 (카메라 1~4)
  3  1초
  4  로봇 뒤집기
  5  2차 검사 (서보 180° 후 카메라 3·4)

집기·투입·회수는 사람이 한다. 촬영이 끝나면 crop_다음번호로 넘어간다.

파일 이름은 crop_번호_차수_카메라 다. crop_1_1_3.jpg 는 1번 샘플 1차 3번 카메라.
기본 저장은 ~/darkroom_captures/cropset 이고, UI에서 경로를 바꿀 수 있다.
"""
from __future__ import annotations

import re
import shutil
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import StringVar, filedialog, messagebox

from arduino_link import resolve_port, resolve_servo_port, send_ok
from camera import grab_stills
from dataset_label import (
    CROPSET_DIR,
    list_crops,
    load_collect_root,
    next_crop_name,
    save_collect_root,
)
from light_tone import LIGHT_TONE_K, light_command
from servo import home as servo_home
from servo import rotate_180 as servo_rotate_180
from skills import task_flip

FIRST_CAMS = (1, 2, 3, 4)
SECOND_CAMS = (3, 4)
TOTAL_SHOTS = len(FIRST_CAMS) + len(SECOND_CAMS)
STAGE_CAMS = {1: FIRST_CAMS, 2: SECOND_CAMS}

LED_ON = light_command()
LED_OFF = "OFF"
LIGHT_SETTLE = 0.5
STAGE_GAP = 1.0

SAFE_NAME = re.compile(r"[^0-9A-Za-z가-힣_-]+")


def run_stage(stage, folder, stem, log=print):
    """한 차수를 통째로 실행한다.

    2차는 서보 180° → 조명 ON → 촬영 → 조명 OFF → 서보 0° 복귀.
    1차는 서보를 안 건드리고 조명·촬영만 한다.

    UI 버튼과 스크립트가 같은 순서를 쓰도록 여기 한 곳에 모아둔다.
    조명과 서보는 촬영이 실패해도 반드시 되돌린다.
    """
    cams = STAGE_CAMS[stage]
    if stage == 2:
        servo_rotate_180()
        log("서보 180° OK")
    try:
        send_ok(LED_ON)
        try:
            time.sleep(LIGHT_SETTLE)
            manifest = grab_stills(
                f"{stage}차",
                cam_ids=cams,
                folder=folder,
                stem=stem,
                manifest_name=f"manifest_{stage}.json",
                ai_only=True,
            )
        finally:
            send_ok(LED_OFF)
    finally:
        if stage == 2:
            servo_home()
            log("서보 원위치 0° OK")
    from sample_roi import SampleNotFound, crop_and_replace

    for cam in manifest["cameras"]:
        path = cam.get("file")
        if not path:
            continue
        try:
            result = crop_and_replace(path, int(cam["id"]), stage=stage, sample_only=True)
            log(f"{Path(path).name}  샘플면 {result.size[0]}x{result.size[1]}  {result.source}")
        except SampleNotFound as exc:
            log(f"{Path(path).name}  샘플 면 없음 — 원본 유지  ({exc})")
        except Exception as exc:
            log(f"{Path(path).name}  크롭 실패 — {exc}")
    return manifest


def next_sample_name(root=None):
    """저장 폴더의 다음 crop_N. crop_1, crop_2, ..."""
    return next_crop_name(root)


class CollectUi:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("데이터 수집")
        self.root.geometry("980x860")
        self.busy = False
        self.preview_photos = []
        self._saved_root = load_collect_root()
        self._saved_root.mkdir(parents=True, exist_ok=True)

        self.path_var = StringVar(value=str(self._saved_root))
        self.sample = StringVar(value=next_sample_name(self._saved_root))
        self.status = StringVar(value="시료를 올리고 데이터 수집 시작을 누르세요.")
        self.progress = StringVar(value=f"0 / {TOTAL_SHOTS} 장")
        self.done = {1: False, 2: False}

        self._build()
        self.sample.trace_add("write", self._sync_progress)
        self._sync_progress()
        self._log(f"저장 위치  {self._saved_root}  (crop_1, crop_2, …)")
        self._log(f"조명 {resolve_port()}  ·  서보 {resolve_servo_port()}  ·  {LIGHT_TONE_K}K {LED_ON}")
        threading.Thread(target=self._warmup_seg, daemon=True).start()

    def _build(self):
        head = tk.Frame(self.root)
        head.pack(fill="x", padx=14, pady=(14, 4))
        tk.Label(head, text="데이터 수집", font=("Arial", 15, "bold")).pack(side="left")
        tk.Label(
            head,
            textvariable=self.progress,
            font=("Arial", 13, "bold"),
            fg="#b45309",
        ).pack(side="right")

        tk.Label(
            self.root,
            text=(
                "사람이 넣고 뺌   ·   자동: 1차 촬영 → 뒤집기 → 2차 촬영   ·   "
                f"샘플당 {TOTAL_SHOTS}장"
            ),
            fg="#555",
        ).pack(anchor="w", padx=14)
        tk.Label(
            self.root,
            text="파일명  crop_번호_차수_카메라   ·   crop_1_1_3.jpg = 1번 샘플 · 1차 · 카메라 3",
            fg="#555",
        ).pack(anchor="w", padx=14)

        path_row = tk.Frame(self.root)
        path_row.pack(fill="x", padx=14, pady=(8, 0))
        tk.Label(path_row, text="저장 경로", font=("Arial", 11)).pack(side="left")
        path_entry = tk.Entry(path_row, textvariable=self.path_var, font=("Arial", 10))
        path_entry.pack(side="left", fill="x", expand=True, padx=8)
        path_entry.bind("<Return>", self._apply_path)
        path_entry.bind("<FocusOut>", self._apply_path)
        tk.Button(path_row, text="경로 변경", command=self._pick_path).pack(side="left")

        name_row = tk.Frame(self.root)
        name_row.pack(fill="x", padx=14, pady=10)
        tk.Label(name_row, text="샘플 이름", font=("Arial", 11)).pack(side="left")
        tk.Entry(name_row, textvariable=self.sample, width=24, font=("Arial", 11)).pack(side="left", padx=8)
        tk.Button(name_row, text="이전 샘플", command=self._prev_sample).pack(side="left")
        tk.Button(name_row, text="다음 샘플", command=self._next_sample).pack(side="left", padx=(4, 0))
        tk.Button(name_row, text="폴더 열기", command=self._open_folder).pack(side="left", padx=6)
        tk.Button(
            name_row,
            text="현재 샘플 삭제",
            bg="#fecaca",
            command=self._delete_sample,
        ).pack(side="left", padx=6)

        buttons = tk.Frame(self.root)
        buttons.pack(pady=12)
        self.collect_btn = tk.Button(
            buttons,
            text="데이터 수집 시작",
            width=28,
            height=3,
            bg="#bfdbfe",
            font=("Arial", 13, "bold"),
            command=self._run_cycle,
        )
        self.collect_btn.pack()

        tk.Label(
            self.root,
            text="Geti로 샘플 면만 남긴다. 끝나면 다음 crop 번호로 넘어간다. 운영 UI와 동시에 켜지 말 것.",
            fg="#555",
        ).pack(anchor="w", padx=14)

        tk.Label(self.root, textvariable=self.status, wraplength=940, justify="left").pack(
            anchor="w", padx=14, pady=(4, 8)
        )
        prev = tk.Frame(self.root)
        prev.pack(fill="x", padx=14, pady=(0, 8))
        tk.Label(prev, text="샘플 면", fg="#555").pack(anchor="w")
        self.preview_row = tk.Frame(prev)
        self.preview_row.pack(fill="x")

        box = tk.Frame(self.root)
        box.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        scroll = tk.Scrollbar(box)
        scroll.pack(side="right", fill="y")
        self.log_box = tk.Text(box, height=12, yscrollcommand=scroll.set, bg="#111", fg="#ddd")
        self.log_box.pack(fill="both", expand=True)
        scroll.config(command=self.log_box.yview)

    def _warmup_seg(self):
        try:
            from sample_ov import available, warmup_safe, weights_path

            if available() and warmup_safe():
                self.root.after(0, lambda: self._log(f"Geti 샘플면  {weights_path()}"))
            else:
                self.root.after(0, lambda: self._log("Geti 모델 없음 — YOLO 세그로 샘플 면을 찾습니다."))
        except Exception as exc:
            self.root.after(0, lambda m=str(exc): self._log(f"Geti 준비 실패 — {m}"))

    def _show_previews(self, folder):
        from dataset_label import FACES, shot_path
        from PIL import Image, ImageTk

        for child in self.preview_row.winfo_children():
            child.destroy()
        self.preview_photos = []
        for face in FACES:
            path = shot_path(folder, face["stage"], face["cam"])
            box = tk.Frame(self.preview_row)
            box.pack(side="left", padx=3)
            tk.Label(box, text=face["name"], fg="#555", font=("Arial", 8)).pack()
            if not path.is_file():
                tk.Label(box, text="없음", bg="#111", fg="#888", width=12, height=5).pack()
                continue
            im = Image.open(path).convert("RGB")
            im.thumbnail((140, 90), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(im)
            self.preview_photos.append(photo)
            tk.Label(box, image=photo, bg="#111").pack()

    def _log(self, text):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.insert("end", f"[{stamp}] {text}\n")
        self.log_box.see("end")

    def _collect_root(self):
        return Path(self.path_var.get().strip() or str(self._saved_root)).expanduser()

    def _sample_dir(self):
        name = SAFE_NAME.sub("_", self.sample.get().strip())
        if not name:
            return None
        return self._collect_root() / name

    def _sample_names(self):
        return [p.name for p in list_crops(self._collect_root())]

    def _pick_path(self):
        if self.busy:
            return
        current = self._collect_root()
        picked = filedialog.askdirectory(
            title="수집 저장 경로",
            initialdir=str(current if current.exists() else CROPSET_DIR),
        )
        if not picked:
            return
        self.path_var.set(picked)
        self._apply_path()

    def _apply_path(self, *_):
        raw = self.path_var.get().strip()
        if not raw:
            self.path_var.set(str(self._saved_root))
            return
        path = Path(raw).expanduser()
        if path == self._saved_root:
            return
        path.mkdir(parents=True, exist_ok=True)
        self._saved_root = save_collect_root(path)
        self.path_var.set(str(self._saved_root))
        self.sample.set(next_sample_name(self._saved_root))
        self.status.set(f"저장 경로를 바꿨습니다. {self._saved_root}")
        self._log(f"저장 경로  {self._saved_root}")
        self._sync_progress()

    def _prev_sample(self):
        if self.busy:
            return
        names = self._sample_names()
        if not names:
            return
        current = SAFE_NAME.sub("_", self.sample.get().strip())
        if current in names:
            index = max(0, names.index(current) - 1)
        else:
            index = len(names) - 1
        self.sample.set(names[index])
        self.status.set(f"{names[index]} — 다시 찍으려면 데이터 수집 시작을 누르세요.")
        self._log(f"— {names[index]} —")

    def _next_sample(self):
        if self.busy:
            return
        names = self._sample_names()
        current = SAFE_NAME.sub("_", self.sample.get().strip())
        if current in names:
            index = names.index(current) + 1
            if index < len(names):
                self.sample.set(names[index])
                self.status.set(f"{names[index]} — 다시 찍으려면 데이터 수집 시작을 누르세요.")
                self._log(f"— {names[index]} —")
                return
        nxt = next_sample_name(self._collect_root())
        self.sample.set(nxt)
        dest = self._collect_root() / nxt
        self.status.set(f"새 샘플 {nxt}. 시료를 올리고 데이터 수집 시작을 누르세요.")
        self._log(f"— 새 샘플 {nxt} → {dest} —")

    def _open_folder(self):
        import subprocess

        target = self._sample_dir()
        if target is None:
            return
        target.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(target)])

    def _clear_previews(self):
        for child in self.preview_row.winfo_children():
            child.destroy()
        self.preview_photos = []

    def _delete_sample(self):
        if self.busy:
            return
        folder = self._sample_dir()
        if folder is None:
            messagebox.showwarning("샘플 이름", "샘플 이름을 입력하세요.")
            return
        root = self._collect_root().resolve()
        target = folder.expanduser().resolve()
        if not target.is_dir():
            messagebox.showinfo("삭제", f"{folder.name} 폴더가 아직 없습니다.")
            return
        if target == root:
            messagebox.showerror("삭제 불가", "저장 경로 자체는 지우지 않습니다.")
            return
        try:
            target.relative_to(root)
        except ValueError:
            messagebox.showerror("삭제 불가", "저장 경로 밖의 폴더는 지우지 않습니다.")
            return
        if not messagebox.askyesno(
            "현재 샘플 삭제",
            f"{folder.name} 폴더를 통째로 삭제할까요?\n{target}",
        ):
            return
        shutil.rmtree(target)
        self._clear_previews()
        nxt = next_sample_name(self._collect_root())
        self.sample.set(nxt)
        self.status.set(f"{folder.name} 삭제. 다음 촬영은 {nxt}")
        self._log(f"{folder.name} 삭제 → {target}")
        self._sync_progress()

    def _shot_count(self):
        return sum(len(STAGE_CAMS[stage]) for stage, ok in self.done.items() if ok)

    def _sync_progress(self, *_):
        """이름을 바꾸면 그 샘플이 어디까지 찍혔는지 파일에서 읽어 표시한다.

        창을 다시 열거나 예전 샘플 이름을 넣었을 때 0/6 으로 보이면
        이미 찍은 걸 또 찍게 된다.
        """
        folder = self._sample_dir()
        for stage in STAGE_CAMS:
            self.done[stage] = bool(
                folder and any(folder.glob(f"{folder.name}_{stage}_[0-9].jpg"))
            )
        self.progress.set(f"{self._shot_count()} / {TOTAL_SHOTS} 장")
        if folder and self._shot_count():
            self._show_previews(folder)

    def _ready_cycle(self):
        """찍기 전에 이름과 덮어쓰기를 확인한다."""
        if self.busy:
            return None
        folder = self._sample_dir()
        if folder is None:
            messagebox.showwarning("샘플 이름", "샘플 이름을 입력하세요.")
            return None
        if folder.name.startswith("sample_") or folder.name.startswith("ok_"):
            keep = messagebox.askyesno(
                "예전 이름",
                f"{folder.name} 은 예전 수집 이름입니다.\n"
                "새 수집은 crop_1, crop_2 처럼 저장합니다. 그래도 이 폴더를 찍을까요?",
            )
            if not keep:
                return None
        if any(folder.glob(f"{folder.name}_[12]_*.jpg")):
            again = messagebox.askyesno(
                "다시 찍기",
                f"{folder.name} 사진이 이미 있습니다.\n덮어쓸까요?",
            )
            if not again:
                return None
        return folder

    def _start(self, worker):
        self.busy = True
        self.collect_btn.config(state="disabled")

        def wrapped():
            try:
                worker()
            except Exception as exc:
                self.root.after(0, lambda m=str(exc): self._failed(m))
            finally:
                self.root.after(0, self._finish)

        threading.Thread(target=wrapped, daemon=True).start()

    def _finish(self):
        self.busy = False
        self.collect_btn.config(state="normal")

    def _failed(self, message):
        self.status.set(f"실패: {message}")
        self._log(f"실패: {message}")

    def _shoot(self, stage, folder, stem):
        self.root.after(0, lambda: self.status.set(f"{stage}차 진행 중... 조명 {LED_ON}"))
        return run_stage(
            stage,
            folder,
            stem,
            log=lambda text: self.root.after(0, lambda t=text: self._log(f"  {t}")),
        )

    def _done_stage(self, stage, folder):
        self.done[stage] = True
        self.progress.set(f"{self._shot_count()} / {TOTAL_SHOTS} 장")
        self._log(f"{stage}차 완료  {folder}")
        self._show_previews(folder)
        if stage == 1:
            self.status.set("1차 완료. 뒤집기 후 2차 촬영합니다.")

    def _finish_cycle(self, folder):
        self.done[1] = True
        self.done[2] = True
        self.progress.set(f"{TOTAL_SHOTS} / {TOTAL_SHOTS} 장")
        self._log(f"수집 완료  {folder}")
        self._show_previews(folder)
        nxt = next_sample_name(self._collect_root())
        self.sample.set(nxt)
        self.status.set(
            f"{folder.name} 저장 완료. 시료를 빼고 다음을 올린 뒤 "
            f"데이터 수집 시작을 누르세요. 다음 {nxt}"
        )
        self._log(f"다음 샘플 {nxt}")

    def _run_cycle(self):
        folder = self._ready_cycle()
        if folder is None:
            return
        self._log(
            f"수집 시작  {folder.name}  "
            f"1차 {list(FIRST_CAMS)} → 뒤집기 → 2차 {list(SECOND_CAMS)}"
        )

        def worker():
            self._shoot(1, folder, f"{folder.name}_1_{{cam}}")
            self.root.after(0, lambda: self._done_stage(1, folder))
            time.sleep(STAGE_GAP)
            self.root.after(0, lambda: self.status.set("뒤집는 중..."))
            self.root.after(0, lambda: self._log("뒤집기"))
            task_flip()
            self.root.after(0, lambda: self._log("뒤집기 OK"))
            self._shoot(2, folder, f"{folder.name}_2_{{cam}}")
            self.root.after(0, lambda: self._finish_cycle(folder))

        self._start(worker)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    CollectUi().run()
