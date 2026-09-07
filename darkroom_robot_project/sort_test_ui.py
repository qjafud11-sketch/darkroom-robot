"""양품·불량 분류만 전체 또는 스텝별로 테스트."""
import threading
from datetime import datetime
from tkinter import END, HORIZONTAL, Button, Entry, Frame, IntVar, Label, LabelFrame, Scale, StringVar, Text, Tk

from skills import (
    SCALE_MAX,
    SCALE_MIN,
    SORT_NG,
    SORT_OK,
    list_pose_steps,
    run_pose_step,
    set_speed_scale,
    set_step_logger,
    task_sort_ng,
    task_sort_ok,
)


def _fmt_pose(pose):
    return " ".join(f"J{sid}:{pose[sid]}" for sid in range(1, 7))


class SortTestUi:
    def __init__(self):
        self.root = Tk()
        self.root.title("분류 테스트 — 양품 / 불량")
        self.root.geometry("920x640")

        self.busy = False
        self.scale_var = IntVar(value=100)
        self.scale_text = StringVar(value="100")
        self.status = StringVar(value="양품·불량 분류를 전체 또는 한 스텝씩 실행합니다.")
        self.next_ok = 1
        self.next_ng = 1
        self.ok_step_btns = {}
        self.ng_step_btns = {}
        self.action_btns = []

        self._build()
        set_step_logger(lambda message: self.root.after(0, self._log, message))

    def _build(self):
        Label(
            self.root,
            text="분류 테스트",
            font=("Arial", 14, "bold"),
        ).pack(pady=(10, 2))
        Label(
            self.root,
            text="inspect / collect 와 동시에 켜지 마세요. 팔 포트가 겹칩니다.",
            fg="#666",
        ).pack()

        speed = LabelFrame(self.root, text="속도 배율", padx=10, pady=6)
        speed.pack(fill="x", padx=12, pady=8)
        low, high = int(SCALE_MIN * 100), int(SCALE_MAX * 100)
        Scale(
            speed,
            from_=low,
            to=high,
            resolution=10,
            orient=HORIZONTAL,
            variable=self.scale_var,
            command=self._on_slider,
            showvalue=False,
            length=480,
        ).pack(side="left", fill="x", expand=True)
        Entry(speed, textvariable=self.scale_text, width=6).pack(side="left", padx=8)
        Label(speed, text="%").pack(side="left")
        self.scale_text.trace_add("write", self._on_typed)

        cols = Frame(self.root)
        cols.pack(fill="both", expand=True, padx=12)

        self._build_side(
            cols,
            title="양품 분류  SORT_OK",
            steps=SORT_OK,
            side="left",
            color="#dcfce7",
            full_cmd=lambda: self._run_full("양품", task_sort_ok),
            next_cmd=lambda: self._run_next("ok"),
            reset_cmd=lambda: self._reset("ok"),
            step_store=self.ok_step_btns,
            kind="ok",
        )
        self._build_side(
            cols,
            title="불량 분류  SORT_NG",
            steps=SORT_NG,
            side="right",
            color="#fee2e2",
            full_cmd=lambda: self._run_full("불량", task_sort_ng),
            next_cmd=lambda: self._run_next("ng"),
            reset_cmd=lambda: self._reset("ng"),
            step_store=self.ng_step_btns,
            kind="ng",
        )

        Label(self.root, textvariable=self.status, fg="#1e3a8a").pack(pady=(4, 0))

        log_box = LabelFrame(self.root, text="로그", padx=8, pady=8)
        log_box.pack(fill="both", expand=True, padx=12, pady=8)
        self.log_text = Text(log_box, height=10, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self._log("준비. 전체 실행 또는 스텝 번호 버튼을 누르세요.")

    def _build_side(self, parent, title, steps, side, color, full_cmd, next_cmd, reset_cmd, step_store, kind):
        box = LabelFrame(parent, text=title, padx=8, pady=8, bg=color)
        box.pack(side=side, fill="both", expand=True, padx=6)

        row = Frame(box, bg=color)
        row.pack(fill="x", pady=(0, 8))
        b1 = Button(row, text="전체 실행", width=10, height=2, command=full_cmd)
        b2 = Button(row, text="다음 스텝", width=10, height=2, command=next_cmd)
        b3 = Button(row, text="처음부터", width=8, command=reset_cmd)
        b1.pack(side="left", padx=2)
        b2.pack(side="left", padx=2)
        b3.pack(side="left", padx=2)
        self.action_btns.extend([b1, b2, b3])

        items = list_pose_steps(steps)
        for index, speeds, pose, name in items:
            label = name or f"{index}번"
            text = f"{index}. {label}\n{_fmt_pose(pose)}"
            btn = Button(
                box,
                text=text,
                anchor="w",
                justify="left",
                height=2,
                command=lambda i=index, k=kind: self._run_one(k, i),
            )
            btn.pack(fill="x", pady=2)
            step_store[index] = btn

        if kind == "ok":
            self.ok_hint = StringVar(value=f"다음: 1 / {len(items)}")
            Label(box, textvariable=self.ok_hint, bg=color).pack(anchor="w", pady=(6, 0))
        else:
            self.ng_hint = StringVar(value=f"다음: 1 / {len(items)}")
            Label(box, textvariable=self.ng_hint, bg=color).pack(anchor="w", pady=(6, 0))

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

    def _log(self, message):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(END, f"[{stamp}] {message}\n")
        self.log_text.see(END)

    def _apply_scale(self):
        try:
            percent = float(self.scale_text.get())
        except ValueError:
            raise ValueError("속도 배율이 숫자가 아닙니다.")
        return set_speed_scale(percent)

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for btn in self.action_btns:
            btn.config(state=state)
        for store in (self.ok_step_btns, self.ng_step_btns):
            for btn in store.values():
                btn.config(state=state)

    def _reset(self, kind):
        if kind == "ok":
            self.next_ok = 1
            total = len(list_pose_steps(SORT_OK))
            self.ok_hint.set(f"다음: 1 / {total}")
            self._log("양품 스텝 포인터를 1로 되돌렸습니다.")
        else:
            self.next_ng = 1
            total = len(list_pose_steps(SORT_NG))
            self.ng_hint.set(f"다음: 1 / {total}")
            self._log("불량 스텝 포인터를 1로 되돌렸습니다.")

    def _run_full(self, label, fn):
        if self.busy:
            return

        def worker():
            try:
                applied = self._apply_scale()
                self.status.set(f"{label} 전체 실행 중…")
                self._log(f"{label} 전체 시작 (속도 {applied:.0f}%)")
                fn()
                self._log(f"{label} 전체 완료")
                self.status.set(f"{label} 전체 완료")
            except Exception as exc:
                self._log(f"{label} 실패: {exc}")
                self.status.set(f"{label} 실패: {exc}")
            finally:
                self.root.after(0, lambda: self._set_busy(False))

        self._set_busy(True)
        threading.Thread(target=worker, daemon=True).start()

    def _run_next(self, kind):
        step = self.next_ok if kind == "ok" else self.next_ng
        self._run_one(kind, step)

    def _run_one(self, kind, step_no):
        if self.busy:
            return
        title = "양품 분류" if kind == "ok" else "불량 분류"
        steps = SORT_OK if kind == "ok" else SORT_NG
        items = list_pose_steps(steps)
        if step_no < 1 or step_no > len(items):
            self._log(f"{title}: 더 이상 스텝이 없습니다. 처음부터을 누르세요.")
            return

        index, speeds, pose, name = items[step_no - 1]
        label = name or f"{index}번"

        def worker():
            try:
                applied = self._apply_scale()
                self.status.set(f"{title} {index}/{len(items)} {label}")
                self._log(f"{title} 스텝 {index} {label}  ({_fmt_pose(pose)}, 속도 {applied:.0f}%)")
                run_pose_step(title, steps, step_no)
                self._log(f"{title} 스텝 {index} 완료")
                self.status.set(f"{title} 스텝 {index} 완료 — 확인 후 다음")
                nxt = step_no + 1
                if kind == "ok":
                    self.next_ok = nxt if nxt <= len(items) else 1
                    self.ok_hint.set(f"다음: {self.next_ok} / {len(items)}")
                else:
                    self.next_ng = nxt if nxt <= len(items) else 1
                    self.ng_hint.set(f"다음: {self.next_ng} / {len(items)}")
            except Exception as exc:
                self._log(f"{title} 스텝 {index} 실패: {exc}")
                self.status.set(str(exc))
            finally:
                self.root.after(0, lambda: self._set_busy(False))

        self._set_busy(True)
        threading.Thread(target=worker, daemon=True).start()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    SortTestUi().run()
