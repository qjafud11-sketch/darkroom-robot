"""비지도 양품 수집 — 사람이 시료를 넣고 빼면, 버튼 한 번에 6장을 찍는다.

파이프라인 2~5번만 자동이다.
  2  1차 검사 (카메라 1~4 = 면 1~4)
  3  1초
  4  로봇 뒤집기
  5  2차 검사 (서보 180° 후 카메라 3·4 = 면 5·6)

집기·투입·회수는 사람이 한다. 촬영이 끝나면 ok_다음번호로 넘어간다.

파일 이름은 ok_번호_차수_카메라 다. ok_1_1_4.jpg 는 1번 샘플의 면 4.
자동으로 돌리면 모든 면이 짝이 바뀌므로, 홀수는 그대로 두고
짝수 샘플은 (1↔2) (3↔5) (4↔6) 로 저장한다.
기본 저장은 ~/darkroom_captures/unsupset 한 폴더다. 샘플·면 하위 폴더는 만들지 않는다.
매니페스트 JSON 은 unsupset/json 에 둔다. 찍은 뒤 SegNext로 샘플 면만 남긴다.
"""
from __future__ import annotations

import re
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import StringVar, filedialog, messagebox

from arduino_link import resolve_port, resolve_servo_port, send_ok
from camera import grab_stills
from dataset_label import (
    CROPPED_DIR,
    CROPSET_DIR,
    DEFECTSET_DIR,
    FACESET_DIR,
    NEW_PREFIX,
    RAWSET_DIR,
    UNSUPSET_DIR,
    collect_sample_files,
    ensure_collect_dirs,
    face_shot_path,
    list_flat_crop_names,
    load_collect_root,
    next_flat_crop_name,
    park_collect_manifest,
    save_collect_root,
)
from light_tone import LIGHT_TONE_K, light_command
from servo import home as servo_home
from servo import rotate_180 as servo_rotate_180
from skills import task_flip

FIRST_CAMS = (1, 2, 3, 4)
SECOND_CAMS = (3, 4)
SKIP_SAVE = set()
STAGE_CAMS = {1: FIRST_CAMS, 2: SECOND_CAMS}
TOTAL_SHOTS = len(FIRST_CAMS) + len(SECOND_CAMS)

LED_ON = light_command()
LED_OFF = "OFF"
LIGHT_SETTLE = 0.5
STAGE_GAP = 1.0
AUTO_GAP = 1.5

SAFE_NAME = re.compile(r"[^0-9A-Za-z가-힣_-]+")
SAMPLE_INDEX = re.compile(rf"^{NEW_PREFIX}_(\d+)$")


# 홀수: 촬영 슬롯 그대로 저장. 짝수: 반대면이 카메라에 오므로 쌍을 바꿔 저장.
FACE_PAIR_SAVE = {
    (1, 1): (1, 2),
    (1, 2): (1, 1),
    (1, 3): (2, 3),
    (2, 3): (1, 3),
    (1, 4): (2, 4),
    (2, 4): (1, 4),
}


def sample_index(name):
    match = SAMPLE_INDEX.fullmatch(str(name or ""))
    return int(match.group(1)) if match else None


def swap_face_pairs(sample):
    """짝수 샘플은 (1,2) (3,5) (4,6) 저장을 뒤바꾼다."""
    index = sample_index(sample)
    return index is not None and index % 2 == 0


def save_slot(sample, capture_stage, cam_id):
    key = (int(capture_stage), int(cam_id))
    if swap_face_pairs(sample):
        return FACE_PAIR_SAVE.get(key, key)
    return key


def collect_stem(sample, capture_stage):
    def stem(cam_id):
        stage, cam = save_slot(sample, capture_stage, cam_id)
        return f"{sample}_{stage}_{cam}"

    return stem


def run_stage(stage, folder, stem, log=print, sample=None):
    """한 차수를 통째로 실행한다.

    2차는 서보 180° → 조명 ON → 촬영 → 조명 OFF → 서보 원위치 8° 복귀.
    1차는 서보를 안 건드리고 조명·촬영만 한다.

    UI 버튼과 스크립트가 같은 순서를 쓰도록 여기 한 곳에 모아둔다.
    조명과 서보는 촬영이 실패해도 반드시 되돌린다.
    """
    cams = STAGE_CAMS[stage]
    if sample and swap_face_pairs(sample):
        log("짝수 샘플 — (1↔2) (3↔5) (4↔6) 로 저장")
    if stage == 2:
        servo_rotate_180()
        log("서보 180° OK")
    prefix = sample or Path(folder).name
    try:
        send_ok(LED_ON)
        try:
            time.sleep(LIGHT_SETTLE)
            manifest = grab_stills(
                f"{stage}차",
                cam_ids=cams,
                folder=folder,
                stem=stem,
                manifest_name=f"{prefix}_manifest_{stage}.json",
                ai_only=True,
            )
        finally:
            send_ok(LED_OFF)
    finally:
        if stage == 2:
            servo_home()
            log("서보 원위치 8° OK")
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
    parked = park_collect_manifest(folder, f"{prefix}_manifest_{stage}.json")
    if parked is not None:
        log(f"json/{parked.name}  보관")
    return manifest


def next_sample_name(root=None):
    """저장 폴더의 다음 ok_N. ok_1, ok_2, ..."""
    return next_flat_crop_name(root, prefix=NEW_PREFIX)


class CollectUi:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("비지도 데이터 수집")
        self.root.geometry("980x860")
        self.busy = False
        self.auto_run = False
        self.stop_after = False
        self.preview_photos = []
        saved = load_collect_root()
        old_roots = {
            CROPSET_DIR.resolve(),
            RAWSET_DIR.resolve(),
            CROPPED_DIR.resolve(),
            FACESET_DIR.resolve(),
        }
        if saved.resolve() in old_roots or saved.resolve() == DEFECTSET_DIR.resolve():
            saved = save_collect_root(UNSUPSET_DIR)
        self._saved_root = ensure_collect_dirs(saved)

        self.path_var = StringVar(value=str(self._saved_root))
        self.sample = StringVar(value=next_sample_name(self._saved_root))
        self.status = StringVar(value="시료를 올리고 데이터 수집 시작을 누르세요.")
        self.progress = StringVar(value=f"0 / {TOTAL_SHOTS} 장")
        self.done = {1: False, 2: False}

        self._build()
        self.sample.trace_add("write", self._sync_progress)
        self._sync_progress()
        self._log(f"저장 위치  {self._saved_root}  ·  JSON  {self._saved_root / 'json'}")
        self._log(f"조명 {resolve_port()}  ·  서보 {resolve_servo_port()}  ·  {LIGHT_TONE_K}K {LED_ON}")
        threading.Thread(target=self._warmup_seg, daemon=True).start()

    def _build(self):
        head = tk.Frame(self.root)
        head.pack(fill="x", padx=14, pady=(14, 4))
        tk.Label(head, text="비지도 양품 수집", font=("Arial", 15, "bold")).pack(side="left")
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
            text="한 폴더에 ok_번호_차_캠.jpg · JSON은 json/  ·  6면 전부 · 짝수는 (1↔2)(3↔5)(4↔6) 저장",
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
            width=22,
            height=3,
            bg="#bfdbfe",
            font=("Arial", 13, "bold"),
            command=self._run_cycle,
        )
        self.collect_btn.pack(side="left", padx=6)
        self.auto_btn = tk.Button(
            buttons,
            text="자동",
            width=10,
            height=3,
            bg="#bbf7d0",
            font=("Arial", 13, "bold"),
            command=self._start_auto,
        )
        self.auto_btn.pack(side="left", padx=6)
        self.stop_btn = tk.Button(
            buttons,
            text="멈춤",
            width=10,
            height=3,
            bg="#fecaca",
            font=("Arial", 13, "bold"),
            state="disabled",
            command=self._request_stop,
        )
        self.stop_btn.pack(side="left", padx=6)

        tk.Label(
            self.root,
            text="사진은 한 폴더에 모은다. 자동은 사이클을 이어서 돌고, 멈춤은 지금 사이클이 끝난 뒤 멈춘다.",
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
                self.root.after(0, lambda: self._log(f"크롭 모델  {weights_path()}"))
            else:
                self.root.after(0, lambda: self._log("크롭 모델 없음 — YOLO 세그로 샘플 면을 찾습니다."))
        except Exception as exc:
            self.root.after(0, lambda m=str(exc): self._log(f"크롭 모델 준비 실패 — {m}"))

    def _show_previews(self, folder, sample=None):
        from dataset_label import FACES
        from PIL import Image, ImageTk

        for child in self.preview_row.winfo_children():
            child.destroy()
        self.preview_photos = []
        name = sample or folder.name
        for face in FACES:
            if (int(face["stage"]), int(face["cam"])) in SKIP_SAVE:
                continue
            path = face_shot_path(folder, face["stage"], face["cam"], name)
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

    def _sample_name(self):
        name = SAFE_NAME.sub("_", self.sample.get().strip())
        return name or None

    def _sample_names(self):
        return list_flat_crop_names(self._collect_root())

    def _locked(self):
        return self.busy or self.auto_run

    def _pick_path(self):
        if self._locked():
            return
        current = self._collect_root()
        picked = filedialog.askdirectory(
            title="수집 저장 경로",
            initialdir=str(current if current.exists() else UNSUPSET_DIR),
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
        path = ensure_collect_dirs(path)
        self._saved_root = save_collect_root(path)
        self.path_var.set(str(self._saved_root))
        self.sample.set(next_sample_name(self._saved_root))
        self.status.set(f"저장 경로를 바꿨습니다. {self._saved_root}")
        self._log(f"저장 경로  {self._saved_root}")
        self._sync_progress()

    def _prev_sample(self):
        if self._locked():
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
        if self._locked():
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
        self.status.set(f"새 샘플 {nxt}. 시료를 올리고 데이터 수집 시작을 누르세요.")
        self._log(f"— 새 샘플 {nxt} → {self._collect_root()} —")

    def _open_folder(self):
        import subprocess

        target = self._collect_root()
        target.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(target)])

    def _clear_previews(self):
        for child in self.preview_row.winfo_children():
            child.destroy()
        self.preview_photos = []

    def _delete_sample(self):
        if self._locked():
            return
        name = self._sample_name()
        if name is None:
            messagebox.showwarning("샘플 이름", "샘플 이름을 입력하세요.")
            return
        root = self._collect_root()
        files = collect_sample_files(root, name)
        if not files:
            messagebox.showinfo("삭제", f"{name} 파일이 아직 없습니다.")
            return
        if not messagebox.askyesno(
            "현재 샘플 삭제",
            f"{name} 파일 {len(files)}개를 삭제할까요?\n{root}",
        ):
            return
        for path in files:
            path.unlink(missing_ok=True)
        self._clear_previews()
        nxt = next_sample_name(self._collect_root())
        self.sample.set(nxt)
        self.status.set(f"{name} 삭제. 다음 촬영은 {nxt}")
        self._log(f"{name} 삭제 → {root}")
        self._sync_progress()

    def _shot_count(self):
        return sum(len(STAGE_CAMS[stage]) for stage, ok in self.done.items() if ok)

    def _sync_progress(self, *_):
        """이름을 바꾸면 그 샘플이 어디까지 찍혔는지 파일에서 읽어 표시한다.

        창을 다시 열거나 예전 샘플 이름을 넣었을 때 0/6 으로 보이면
        이미 찍은 걸 또 찍게 된다.
        """
        name = self._sample_name()
        root = self._collect_root()
        files = collect_sample_files(root, name) if name else []
        for stage in STAGE_CAMS:
            self.done[stage] = bool(
                name and any(p.name.startswith(f"{name}_{stage}_") and p.suffix == ".jpg" for p in files)
            )
        self.progress.set(f"{self._shot_count()} / {TOTAL_SHOTS} 장")
        if name and self._shot_count():
            self._show_previews(root, sample=name)

    def _ready_cycle(self):
        """찍기 전에 이름과 덮어쓰기를 확인한다."""
        if self.busy:
            return None
        name = self._sample_name()
        if name is None:
            messagebox.showwarning("샘플 이름", "샘플 이름을 입력하세요.")
            return None
        root = self._collect_root()
        if name.startswith("sample_") or name.startswith("crop_"):
            keep = messagebox.askyesno(
                "예전 이름",
                f"{name} 은 결함/크롭 수집 이름입니다.\n"
                "비지도 수집은 ok_1, ok_2 처럼 저장합니다. 그래도 이 이름으로 찍을까요?",
            )
            if not keep:
                return None
        if any(p.suffix == ".jpg" for p in collect_sample_files(root, name)):
            again = messagebox.askyesno(
                "다시 찍기",
                f"{name} 사진이 이미 있습니다.\n덮어쓸까요?",
            )
            if not again:
                return None
        return name

    def _set_run_buttons(self, running):
        self.collect_btn.config(state="disabled" if running else "normal")
        self.auto_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if self.auto_run and running else "disabled")

    def _start(self, worker):
        self.busy = True
        self._set_run_buttons(True)

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
        if self.auto_run and not self.stop_after:
            self.status.set(f"다음 사이클까지 {AUTO_GAP:.1f}초. 시료를 바꿔 두세요.")
            self.root.after(int(AUTO_GAP * 1000), self._continue_auto)
            return
        if self.auto_run and self.stop_after:
            self._log("자동 멈춤")
            self.status.set("자동을 멈췄습니다. 시료를 올리고 다시 시작하세요.")
        self.auto_run = False
        self.stop_after = False
        self._set_run_buttons(False)

    def _failed(self, message):
        self.auto_run = False
        self.stop_after = False
        self.status.set(f"실패: {message}")
        self._log(f"실패: {message}")

    def _start_auto(self):
        if self.busy or self.auto_run:
            return
        name = self._ready_cycle()
        if name is None:
            return
        self.auto_run = True
        self.stop_after = False
        self._log("자동 시작 — 멈춤을 누르면 이 사이클 뒤에 멈춥니다.")
        self._begin_cycle(name)

    def _request_stop(self):
        if not self.auto_run:
            return
        self.stop_after = True
        self.stop_btn.config(state="disabled")
        self.status.set("멈춤 — 지금 사이클이 끝나면 멈춥니다.")
        self._log("멈춤 예약")

    def _next_auto_name(self):
        return next_sample_name(self._collect_root())

    def _continue_auto(self):
        if not self.auto_run or self.stop_after:
            self.auto_run = False
            self.stop_after = False
            self._set_run_buttons(False)
            self.status.set("자동을 멈췄습니다. 시료를 올리고 다시 시작하세요.")
            return
        name = self._next_auto_name()
        self.sample.set(name)
        self._begin_cycle(name)

    def _shoot(self, stage, folder, stem, sample):
        self.root.after(0, lambda: self.status.set(f"{stage}차 진행 중... 조명 {LED_ON}"))
        return run_stage(
            stage,
            folder,
            stem,
            log=lambda text: self.root.after(0, lambda t=text: self._log(f"  {t}")),
            sample=sample,
        )

    def _done_stage(self, stage, folder, sample):
        self.done[stage] = True
        self.progress.set(f"{self._shot_count()} / {TOTAL_SHOTS} 장")
        self._log(f"{stage}차 완료  {folder} / {sample}")
        self._show_previews(folder, sample=sample)
        if stage == 1:
            self.status.set("1차 완료. 뒤집기 후 2차 촬영합니다.")

    def _finish_cycle(self, folder, sample):
        self.done[1] = True
        self.done[2] = True
        self.progress.set(f"{TOTAL_SHOTS} / {TOTAL_SHOTS} 장")
        self._log(f"수집 완료  {folder} / {sample}")
        self._show_previews(folder, sample=sample)
        nxt = next_sample_name(self._collect_root())
        self.sample.set(nxt)
        if self.auto_run and not self.stop_after:
            self.status.set(f"{sample} 저장. 다음 {nxt} — 시료를 바꿔 두세요.")
        else:
            self.status.set(
                f"{sample} 저장 완료. 시료를 빼고 다음을 올린 뒤 "
                f"데이터 수집 시작을 누르세요. 다음 {nxt}"
            )
        self._log(f"다음 샘플 {nxt}")

    def _begin_cycle(self, name):
        folder = ensure_collect_dirs(self._collect_root())
        mode = "자동" if self.auto_run else "1회"
        pair_note = "  ·  짝수 (1↔2)(3↔5)(4↔6)" if swap_face_pairs(name) else "  ·  홀수 면 그대로"
        self._log(
            f"{mode} 수집  {name}  → {folder}  "
            f"1차 {list(FIRST_CAMS)} → 뒤집기 → 2차 {list(SECOND_CAMS)}{pair_note}"
        )

        def worker():
            self._shoot(1, folder, collect_stem(name, 1), name)
            self.root.after(0, lambda: self._done_stage(1, folder, name))
            time.sleep(STAGE_GAP)
            self.root.after(0, lambda: self.status.set("뒤집는 중..."))
            self.root.after(0, lambda: self._log("뒤집기"))
            task_flip()
            self.root.after(0, lambda: self._log("뒤집기 OK"))
            self._shoot(2, folder, collect_stem(name, 2), name)
            self.root.after(0, lambda: self._finish_cycle(folder, name))

        self._start(worker)

    def _run_cycle(self):
        if self.auto_run:
            return
        name = self._ready_cycle()
        if name is None:
            return
        self._begin_cycle(name)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    CollectUi().run()
