"""데이터 수집 — 샘플 하나당 6장을 모은다.

1차는 그대로 4대를 찍고, 2차는 서보를 180° 돌린 뒤 카메라 3·4만 찍는다.
촬영 값은 캘리브(~/darkroom_calib.json)를 그대로 쓴다.
남기는 건 보정본 한 장뿐이다. 어노테이션은 판독 때와 같은 그림에 쳐야 하고,
원본까지 남기면 장수가 두 배가 되면서 어느 쪽에 상자를 쳤는지 헷갈린다.
검사 촬영(camera.grab_stills 기본값)은 반대로 원본도 같이 남긴다.

파일 이름은 샘플_차수_카메라 순서다. sample_001_1_3.jpg 는 1번 샘플 1차 3번 카메라.
이름만 보고 알 수 있으니 차수별 하위 폴더는 두지 않는다.

조명은 캘리브를 맞춘 밝기와 같아야 한다. 다른 밝기로 모으면 그 데이터는 못 쓴다.
"""
from __future__ import annotations

import re
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import StringVar, messagebox

from arduino_link import resolve_port, resolve_servo_port, send_ok
from camera import CAPTURE_DIR, grab_stills
from light_tone import LIGHT_TONE_K, light_command
from servo import home as servo_home
from servo import rotate_180 as servo_rotate_180

DATASET_DIR = CAPTURE_DIR / "dataset"

FIRST_CAMS = (1, 2, 3, 4)
SECOND_CAMS = (3, 4)
TOTAL_SHOTS = len(FIRST_CAMS) + len(SECOND_CAMS)
STAGE_CAMS = {1: FIRST_CAMS, 2: SECOND_CAMS}

LED_ON = light_command()
LED_OFF = "OFF"
# 네오픽셀은 바로 켜지지만, 켜자마자 셔터를 열면 첫 프레임이 어둡게 걸린 적이 있다.
LIGHT_SETTLE = 0.5

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
    for cam in manifest["cameras"]:
        if cam.get("file"):
            log(Path(cam["file"]).name)
    return manifest


def next_sample_name():
    """dataset 폴더를 훑어 sample_### 다음 번호를 고른다."""
    used = set()
    if DATASET_DIR.exists():
        for item in DATASET_DIR.iterdir():
            match = re.fullmatch(r"sample_(\d+)", item.name)
            if item.is_dir() and match:
                used.add(int(match.group(1)))
    number = 1
    while number in used:
        number += 1
    return f"sample_{number:03d}"


class CollectUi:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("데이터 수집")
        self.root.geometry("720x560")
        self.busy = False

        self.sample = StringVar(value=next_sample_name())
        self.status = StringVar(value="샘플 이름을 정하고 1차 검사를 누르세요.")
        self.progress = StringVar(value=f"0 / {TOTAL_SHOTS} 장")
        self.done = {1: False, 2: False}

        self._build()
        self.sample.trace_add("write", self._sync_progress)
        self._sync_progress()
        self._log(f"저장 위치  {DATASET_DIR}")
        self._log(f"조명 {resolve_port()}  ·  서보 {resolve_servo_port()}  ·  {LIGHT_TONE_K}K {LED_ON}")

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
                f"1차 카메라 {', '.join(str(n) for n in FIRST_CAMS)}   ·   "
                f"2차 서보 180° 후 카메라 {', '.join(str(n) for n in SECOND_CAMS)}   ·   "
                f"샘플당 {TOTAL_SHOTS}장"
            ),
            fg="#555",
        ).pack(anchor="w", padx=14)
        tk.Label(
            self.root,
            text="파일명  샘플_차수_카메라   ·   sample_001_1_3.jpg = 1번 샘플 1차 3번 카메라   ·   보정본 한 장만 저장",
            fg="#555",
        ).pack(anchor="w", padx=14)

        name_row = tk.Frame(self.root)
        name_row.pack(fill="x", padx=14, pady=10)
        tk.Label(name_row, text="샘플 이름", font=("Arial", 11)).pack(side="left")
        tk.Entry(name_row, textvariable=self.sample, width=24, font=("Arial", 11)).pack(side="left", padx=8)
        tk.Button(name_row, text="다음 샘플", command=self._next_sample).pack(side="left")
        tk.Button(name_row, text="폴더 열기", command=self._open_folder).pack(side="left", padx=6)

        buttons = tk.Frame(self.root)
        buttons.pack(pady=8)
        self.first_btn = tk.Button(
            buttons,
            text=f"1차 검사\n촬영 {len(FIRST_CAMS)}장",
            width=18,
            height=3,
            bg="#bfdbfe",
            font=("Arial", 12, "bold"),
            command=self._run_first,
        )
        self.first_btn.pack(side="left", padx=10)
        self.second_btn = tk.Button(
            buttons,
            text=f"2차 검사\n서보 180° → {len(SECOND_CAMS)}장",
            width=18,
            height=3,
            bg="#fde68a",
            font=("Arial", 12, "bold"),
            command=self._run_second,
        )
        self.second_btn.pack(side="left", padx=10)

        tk.Label(self.root, textvariable=self.status, wraplength=680, justify="left").pack(
            anchor="w", padx=14, pady=(4, 8)
        )

        box = tk.Frame(self.root)
        box.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        scroll = tk.Scrollbar(box)
        scroll.pack(side="right", fill="y")
        self.log_box = tk.Text(box, height=12, yscrollcommand=scroll.set, bg="#111", fg="#ddd")
        self.log_box.pack(fill="both", expand=True)
        scroll.config(command=self.log_box.yview)

    def _log(self, text):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.insert("end", f"[{stamp}] {text}\n")
        self.log_box.see("end")

    def _sample_dir(self):
        name = SAFE_NAME.sub("_", self.sample.get().strip())
        if not name:
            return None
        return DATASET_DIR / name

    def _next_sample(self):
        if self.busy:
            return
        self.sample.set(next_sample_name())
        self.status.set("새 샘플입니다. 시료를 올리고 1차 검사를 누르세요.")
        self._log(f"— 새 샘플 {self.sample.get()} —")

    def _open_folder(self):
        import subprocess

        target = self._sample_dir()
        if target is None:
            return
        target.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(target)])

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

    def _ready(self, stage):
        """찍기 전에 이름과 덮어쓰기를 확인한다. 폴더와 이름 틀을 돌려준다."""
        if self.busy:
            return None
        folder = self._sample_dir()
        if folder is None:
            messagebox.showwarning("샘플 이름", "샘플 이름을 입력하세요.")
            return None
        # 이름을 여기서 굳혀둔다. 찍는 중에 입력칸을 고쳐도 파일명이 안 갈린다.
        stem = f"{folder.name}_{stage}_{{cam}}"
        if any(folder.glob(f"{folder.name}_{stage}_*.jpg")):
            again = messagebox.askyesno(
                "다시 찍기",
                f"{folder.name} 의 {stage}차 사진이 이미 있습니다.\n덮어쓸까요?",
            )
            if not again:
                return None
        return folder, stem

    def _start(self, worker):
        self.busy = True
        self.first_btn.config(state="disabled")
        self.second_btn.config(state="disabled")

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
        self.first_btn.config(state="normal")
        self.second_btn.config(state="normal")

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
        count = self._shot_count()
        self.progress.set(f"{count} / {TOTAL_SHOTS} 장")
        self._log(f"{stage}차 완료  {folder}")
        if count >= TOTAL_SHOTS:
            self.status.set(f"{folder.name} 완료 — {TOTAL_SHOTS}장. '다음 샘플'을 누르세요.")
        else:
            self.status.set(f"{stage}차 완료. 남은 단계를 진행하세요.")

    def _run_first(self):
        ready = self._ready(1)
        if ready is None:
            return
        folder, stem = ready
        self._log(f"1차 시작  {folder.name}  카메라 {list(FIRST_CAMS)}")

        def worker():
            self._shoot(1, folder, stem)
            self.root.after(0, lambda: self._done_stage(1, folder))

        self._start(worker)

    def _run_second(self):
        ready = self._ready(2)
        if ready is None:
            return
        folder, stem = ready
        self._log(f"2차 시작  {folder.name}  서보 180° 후 카메라 {list(SECOND_CAMS)}")

        def worker():
            self._shoot(2, folder, stem)
            self.root.after(0, lambda: self._done_stage(2, folder))

        self._start(worker)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    CollectUi().run()
