"""NUC 운영 UI — Vision Mate 스타일 · 실행/중지만."""
from __future__ import annotations

import json
import threading
import tkinter as tk

from camera import cam_ids_for_label, grab_stills, start_capture_server
from gui_common import (
    COLORS,
    AppHeader,
    Card,
    InspectWall,
    OperatorPanel,
    PhaseHeader,
    RecordsPage,
    RemoteServer,
    ReportsPage,
    SettingsPage,
    Sidebar,
    TimelineStrip,
    phase_label,
)
from judgment import RESULT_PATH
from pipeline import FULL_SEQUENCE
from ui_store import append_record, format_judged_at, remove_demo_records

HOST = "0.0.0.0"
PORT = 8585
CAPTURE_PORT = 8586

root = None
server = RemoteServer(HOST, PORT)
app_header = None
sidebar = None
body = None
center_stack = None
inspect_page = None
records_page = None
reports_page = None
settings_page = None
operator = None
phase_header = None
timeline = None
inspect_wall = None

is_running = False
stop_requested = False
cycle_stop_requested = False
session_total = 0
session_ok = 0
current_page = "검사"


def _on_ui(fn):
    if root:
        root.after(0, fn)


def _update_stats():
    if operator:
        operator.set_stats(session_total, session_ok)


def apply_snapshot(snapshot):
    if not snapshot:
        return

    def update():
        global session_total, session_ok
        if snapshot.get("captures") and inspect_wall:
            inspect_wall.load_capture_folders(snapshot["captures"])
        if snapshot.get("judgment"):
            if inspect_wall:
                inspect_wall.set_judgment(snapshot["judgment"])
            if operator:
                operator.set_judgment(snapshot["judgment"], inspect_wall)
        verdict = snapshot.get("verdict")
        cmd = snapshot.get("command", "")
        finish = cmd in ("REPORT", "JUDGE")
        if finish and verdict in ("OK", "NG") and is_running:
            session_total += 1
            if verdict == "OK":
                session_ok += 1
            judgment = snapshot.get("judgment") or {}
            append_record(judgment, session_total)
            if records_page:
                records_page.refresh()
            if reports_page:
                reports_page.refresh(scroll=False)
            if operator:
                operator.set_verdict(verdict, judgment)
                operator.set_stats(session_total, session_ok)
            if phase_header:
                phase_header.set_phase("검사 완료", sub=f"결과 {verdict}")

    _on_ui(update)


def on_executor_connect(addr):
    def update():
        if app_header:
            app_header.set_link(f"실행기 {addr[0]}", connected=True)
        _set_controls(idle=not is_running)

    _on_ui(update)


def on_executor_disconnect():
    def update():
        if app_header:
            app_header.set_link("실행기 연결 끊김", connected=False)
        _set_controls(idle=True)

    _on_ui(update)


def _set_controls(idle=True):
    if operator:
        operator.set_controls(idle=idle)


def _prepare_new_run(clear_inspect=None, reset_verdict=True):
    if inspect_wall:
        inspect_wall.clear(inspect=clear_inspect)
    if operator and reset_verdict:
        operator.clear_defects()
        operator.show_idle_verdict()
    if timeline and reset_verdict:
        timeline.reset()
    if phase_header:
        phase_header.set_phase("준비 중", sub="공정을 시작합니다", running=True)


def _on_step_start(command):
    timeline_cmd = "BRINGOUT" if command == "JUDGE" else command
    label = phase_label(command) or phase_label(timeline_cmd)
    if not label:
        return

    def update():
        if timeline:
            timeline.mark_current(timeline_cmd)
        if phase_header:
            phase_header.set_phase(label, sub="", running=True)

    _on_ui(update)


def _on_step_done(resp):
    cmd = resp.get("command", "")
    if timeline and cmd:
        timeline.mark_done("BRINGOUT" if cmd == "JUDGE" else cmd)
    apply_snapshot(resp)


def send_step(cmd):
    if server.conn is None:
        _on_ui(lambda: phase_header.set_phase(
            "실행기 없음", sub="robot_client.py를 실행하세요", error=True,
        ))
        return False

    _on_step_start(cmd)
    try:
        resp = server.send(cmd)
    except Exception as exc:
        _on_ui(lambda: phase_header.set_phase("통신 오류", sub=str(exc), error=True))
        return False

    if resp is None:
        on_executor_disconnect()
        return False

    status = resp.get("status")
    _on_ui(lambda: _on_step_done(resp))

    if status != "DONE":
        _on_ui(lambda: phase_header.set_phase("오류", sub=resp.get("message", ""), error=True))
        return False
    return True


def run_sequence_worker(sequence=None, clear_inspect=None, reset_verdict=True):
    global is_running, stop_requested, cycle_stop_requested
    sequence = sequence or FULL_SEQUENCE
    cycle = 0
    halted = False

    while True:
        cycle += 1
        prepared = threading.Event()

        def prep(cycle_no=cycle, first=cycle == 1):
            _prepare_new_run(
                clear_inspect=clear_inspect if first else None,
                reset_verdict=reset_verdict if first else True,
            )
            if phase_header and cycle_no > 1:
                phase_header.set_phase(
                    "다음 사이클",
                    sub=f"{cycle_no}번째 샘플 — 집기부터 다시 시작합니다",
                    running=True,
                )
            prepared.set()

        _on_ui(prep)
        prepared.wait(timeout=15)

        if stop_requested:
            halted = True
            _on_ui(lambda: phase_header.set_phase(
                "비상정지",
                sub="현재 단계까지 마치고 다음 공정은 실행하지 않았습니다",
            ))
            break

        for cmd in sequence:
            if stop_requested:
                halted = True
                _on_ui(lambda: phase_header.set_phase(
                    "비상정지",
                    sub="현재 단계까지 마치고 다음 공정은 실행하지 않았습니다",
                ))
                break
            if not send_step(cmd):
                halted = True
                break

        if halted or stop_requested:
            break
        if cycle_stop_requested:
            _on_ui(lambda: phase_header.set_phase(
                "검사 중지",
                sub=f"이번 사이클({cycle}번째)을 끝까지 마친 뒤 멈췄습니다",
            ))
            break
        print(f"[UI] 사이클 {cycle} 완료 — 처음부터 다시 시작")

    is_running = False
    stop_requested = False
    cycle_stop_requested = False
    _on_ui(lambda: _set_controls(idle=True))


def start_run(sequence=None, clear_inspect=None, reset_verdict=True):
    global is_running, stop_requested, cycle_stop_requested
    if is_running:
        return
    if current_page != "검사":
        show_page("검사")
    if server.conn is None:
        phase_header.set_phase(
            "실행기 없음", sub="robot_client.py 연결 후 다시 시도", error=True,
        )
        return
    is_running = True
    stop_requested = False
    cycle_stop_requested = False
    _set_controls(idle=False)
    threading.Thread(
        target=run_sequence_worker,
        kwargs={
            "sequence": sequence,
            "clear_inspect": clear_inspect,
            "reset_verdict": reset_verdict,
        },
        daemon=True,
    ).start()


def stop_run():
    """검사 중지 — 이번 사이클 남은 단계를 모두 끝낸 뒤 멈춘다."""
    global cycle_stop_requested
    if not is_running:
        if phase_header:
            phase_header.set_phase("대기", sub="검사가 시작된 뒤에만 중지할 수 있습니다")
        return
    cycle_stop_requested = True
    print("[UI] 검사 중지 — 이번 사이클 끝까지 진행")
    phase_header.set_phase(
        phase_header.phase_var.get(),
        sub="검사 중지 — 이번 사이클을 끝까지 마친 뒤 멈춥니다",
        running=True,
    )


def emergency_stop():
    """비상정지 — 지금 단계가 끝나는 즉시 다음 공정은 하지 않는다."""
    global stop_requested
    if not is_running:
        if phase_header:
            phase_header.set_phase("대기", sub="검사가 시작된 뒤에만 비상정지할 수 있습니다")
        return
    stop_requested = True
    print("[UI] 비상정지 — 현재 단계 종료 후 중단")
    phase_header.set_phase(
        phase_header.phase_var.get(),
        sub="비상정지 — 현재 단계가 끝나면 바로 멈춥니다",
        running=True,
    )


def on_defect_select(event):
    if not inspect_wall or not operator:
        return
    widget = event.widget
    sel = widget.curselection()
    if not sel:
        return
    if widget is getattr(operator, "yolo_list", None):
        items = getattr(operator, "yolo_items", None)
    else:
        items = getattr(operator, "unsup_items", None) or getattr(operator, "list_items", None)
    if items and 0 <= int(sel[0]) < len(items):
        inspect_wall.highlight_item(items[int(sel[0])])
    else:
        inspect_wall.highlight_defect(int(sel[0]))


def load_record_into_inspect(record):
    judgment = record.get("judgment") or {}
    apply_snapshot(
        {
            "verdict": record.get("verdict", judgment.get("verdict", "OK")),
            "judgment": judgment,
            "captures": {
                "1차": judgment.get("manifest_1", ""),
                "2차": judgment.get("manifest_2", ""),
            },
        }
    )
    if operator:
        operator.set_verdict(record.get("verdict"), judgment)
    if phase_header:
        phase_header.set_phase(
            "기록 불러옴",
            sub=f"{format_judged_at(record.get('judged_at', ''))} · Run #{record.get('run_no')} · {record.get('verdict')}",
        )
    show_page("검사")


def open_record_from_report(record):
    """리포트 NG 로그 → 기록 탭 상세."""
    show_page("기록")
    if records_page:
        records_page.refresh()
        records_page._select_record(record)


def load_saved_judgment():
    if not RESULT_PATH.is_file():
        return
    try:
        data = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    apply_snapshot(
        {
            "verdict": data.get("verdict", "OK"),
            "judgment": data,
            "captures": {"1차": data.get("manifest_1", ""), "2차": data.get("manifest_2", "")},
        }
    )
    if operator and data.get("verdict") in ("OK", "NG"):
        operator.set_verdict(data["verdict"], data)
        phase_header.set_phase("검사 완료", sub=f"결과 {data['verdict']}")


def grab_for_pipeline(label):
    manifest = grab_stills(label, cam_ids=cam_ids_for_label(label))
    folder = manifest["folder"]
    if inspect_wall and root:
        root.after(0, lambda: inspect_wall.load_capture_folders({label: folder}))
    return manifest


def show_page(name):
    global current_page
    current_page = name
    if sidebar:
        sidebar.set_active(name)

    for page in (inspect_page, records_page, reports_page, settings_page):
        if page:
            page.pack_forget()

    if name == "검사":
        inspect_page.pack(fill="both", expand=True)
        if operator:
            operator.pack(side="right", fill="y", padx=(0, 12), pady=12)
    elif name == "기록":
        records_page.refresh()
        records_page.pack(fill="both", expand=True)
        if operator:
            operator.pack_forget()
    elif name == "리포트":
        reports_page.refresh()
        reports_page.pack(fill="both", expand=True)
        if operator:
            operator.pack_forget()
    elif name == "설정":
        settings_page.load_values()
        settings_page.pack(fill="both", expand=True)
        if operator:
            operator.pack_forget()


def on_nav(name):
    show_page(name)


def build_ui():
    global root, app_header, sidebar, body, center_stack
    global inspect_page, records_page, reports_page, settings_page
    global phase_header, timeline, operator, inspect_wall

    root = tk.Tk()
    root.title("Darkroom Vision — 운영")
    root.geometry("1520x900")
    root.minsize(1200, 760)
    root.configure(bg=COLORS["bg_root"])

    app_header = AppHeader(root)

    body = tk.Frame(root, bg=COLORS["bg_root"])
    body.pack(fill="both", expand=True)

    sidebar = Sidebar(body, on_nav=on_nav)

    center_stack = tk.Frame(body, bg=COLORS["bg_root"])
    center_stack.pack(side="left", fill="both", expand=True)

    inspect_page = tk.Frame(center_stack, bg=COLORS["bg_root"])
    status_card = Card(inspect_page, padx=14, pady=12)
    status_card.pack(fill="x", padx=8, pady=(8, 4))
    phase_header = PhaseHeader(status_card)
    phase_header.pack(fill="x")
    timeline = TimelineStrip(status_card)
    timeline.pack(fill="x", pady=(10, 0))
    inspect_wall = InspectWall(inspect_page)

    records_page = RecordsPage(
        center_stack,
        on_load_record=load_record_into_inspect,
        on_back=lambda: show_page("검사"),
    )
    reports_page = ReportsPage(
        center_stack,
        on_back=lambda: show_page("검사"),
        on_open_record=open_record_from_report,
    )
    settings_page = SettingsPage(
        center_stack,
        on_back=lambda: show_page("검사"),
    )

    operator = OperatorPanel(
        body,
        on_run=start_run,
        on_stop=stop_run,
        on_estop=emergency_stop,
    )
    operator.bind_defect_select(on_defect_select)
    operator.set_stats(0, 0)
    operator.show_idle_verdict()

    show_page("검사")


def on_close():
    root.destroy()


if __name__ == "__main__":
    build_ui()
    server.on_connect = on_executor_connect
    server.on_disconnect = on_executor_disconnect
    server.start_background()
    start_capture_server(grab_for_pipeline)
    load_saved_judgment()
    remove_demo_records()
    if records_page:
        records_page.refresh()
    if reports_page:
        reports_page.refresh()
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
