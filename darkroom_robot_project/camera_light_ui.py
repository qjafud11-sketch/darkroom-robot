"""카메라 실시간 화면과 방향별 조명을 함께 확인하는 Tkinter UI."""

from __future__ import annotations

import base64
import json
import queue
import re
import socket
import struct
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

from arduino_link import LIGHT_BRIGHTNESS, LIGHT_MAX
from camera import CAMERAS as CAM_SLOTS


PI_HOST = "127.0.0.1"
PI_LIGHT_PORT = 9001
CAPTURE_DIR = Path(__file__).resolve().parent / "captures"
CAMERAS = tuple((cam["name"], cam["device"]) for cam in CAM_SLOTS if cam.get("device"))
# 점등 비교로 확인한 실제 방향 순서. 괄호의 R 번호는 D7 체인의 물리 링 번호다.
LIGHTS = (
    ("전체 조명", "B:{brightness}", "OFF"),
    ("카메라 1 방향 조명", "R2:{brightness}", "R2:0"),
    ("카메라 2 방향 조명", "R4:{brightness}", "R4:0"),
    ("카메라 3 방향 조명", "R3:{brightness}", "R3:0"),
    ("보조 조명", "R1:{brightness}", "R1:0"),
)
CAMERA_LIGHT_SELECTION = (1, 2, 3, 0)


class LightController:
    """라즈베리파이 조명 서버에 명령을 보내는 TCP 클라이언트."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._lock = threading.Lock()

    def send(self, command: str) -> None:
        with self._lock:
            request = json.dumps({"command": command.strip()}) + "\n"
            with socket.create_connection((self.host, self.port), timeout=3) as sock:
                sock.sendall(request.encode("utf-8"))
                response_file = sock.makefile("r", encoding="utf-8")
                line = response_file.readline()
            if not line:
                raise ConnectionError("라즈베리파이 조명 서버가 응답하지 않습니다.")
            response = json.loads(line)
            if response.get("status") != "OK":
                raise RuntimeError(response.get("message", "조명 명령 실패"))

    def close(self) -> None:
        try:
            self.send("OFF")
        except (OSError, ValueError, RuntimeError):
            pass


def _read_exact(stream, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise EOFError("카메라 영상 스트림이 종료되었습니다.")
        chunks.extend(chunk)
    return bytes(chunks)


def read_png(stream) -> bytes:
    """FFmpeg image2pipe에서 PNG 한 프레임을 정확히 분리한다."""
    signature = _read_exact(stream, 8)
    if signature != b"\x89PNG\r\n\x1a\n":
        raise ValueError("카메라 스트림이 PNG 형식이 아닙니다.")
    image = bytearray(signature)
    while True:
        length_bytes = _read_exact(stream, 4)
        length = struct.unpack(">I", length_bytes)[0]
        chunk_type = _read_exact(stream, 4)
        payload_and_crc = _read_exact(stream, length + 4)
        image.extend(length_bytes)
        image.extend(chunk_type)
        image.extend(payload_and_crc)
        if chunk_type == b"IEND":
            return bytes(image)


class LiveCamera:
    """V4L2 영상을 FFmpeg로 받아 Tk에서 표시할 PNG 프레임을 제공한다."""

    def __init__(self):
        self.frames: queue.Queue[bytes] = queue.Queue(maxsize=1)
        self.process: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.device = ""
        self.last_error = ""
        self.running = False

    def start(self, device: str) -> None:
        self.stop()
        self.device = device
        self.last_error = ""
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", "1280x720", "-framerate", "15",
            "-i", device, "-vf", "scale=960:540",
            "-f", "image2pipe", "-vcodec", "png", "-",
        ]
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.running = True
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _reader(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while self.running:
                frame = read_png(self.process.stdout)
                try:
                    self.frames.get_nowait()
                except queue.Empty:
                    pass
                self.frames.put_nowait(frame)
        except (EOFError, ValueError, OSError) as exc:
            if self.running:
                detail = ""
                if self.process.stderr is not None:
                    detail = self.process.stderr.read().decode("utf-8", errors="replace").strip()
                self.last_error = detail.splitlines()[-1] if detail else str(exc)
        finally:
            self.running = False

    def stop(self) -> None:
        self.running = False
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                break


def measure_brightness(image_path: Path) -> dict[str, float]:
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(image_path),
        "-vf", "signalstats,metadata=print:file=-", "-frames:v", "1",
        "-f", "null", "-",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError("촬영은 됐지만 밝기 분석에 실패했습니다.")
    values = {}
    for key in ("YLOW", "YAVG", "YHIGH"):
        match = re.search(rf"lavfi\.signalstats\.{key}=([0-9.]+)", result.stdout)
        if not match:
            raise RuntimeError(f"밝기 분석값 {key}를 찾지 못했습니다.")
        values[key] = float(match.group(1))
    return values


def brightness_label(mean: float) -> tuple[str, str]:
    if mean < 25:
        return "너무 어두움", "#ff6b6b"
    if mean < 55:
        return "어두움", "#ffb347"
    if mean <= 200:
        return "밝기 양호", "#6ee7a8"
    return "과노출 위험", "#ff6b6b"


class CameraLightUi:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("카메라 · 조명 실시간 확인")
        self.root.geometry("1280x800")
        self.root.minsize(1100, 700)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.camera_var = tk.IntVar(value=0)
        self.light_var = tk.IntVar(value=0)
        self.host_var = tk.StringVar(value=PI_HOST)
        self.port_var = tk.StringVar(value=str(PI_LIGHT_PORT))
        self.brightness_var = tk.IntVar(value=LIGHT_BRIGHTNESS)
        self.settle_var = tk.StringVar(value="2.0")
        self.status_var = tk.StringVar(value="카메라를 연결하는 중...")
        self.metric_var = tk.StringVar(value="밝기 측정값: -")
        self.live = LiveCamera()
        self.light: LightController | None = None
        self.preview_image: tk.PhotoImage | None = None
        self.last_frame: bytes | None = None
        self.last_frame_at = 0.0
        self.frame_lock = threading.Lock()
        self.busy = False
        self.buttons: list[tk.Button] = []

        self._build()
        self.root.after(100, self._start_selected_camera)
        self.root.after(50, self._poll_frames)

    def _build(self) -> None:
        body = tk.Frame(self.root, bg="#171717")
        body.pack(fill="both", expand=True)
        controls = tk.Frame(body, width=310, bg="#f3f4f6", padx=12, pady=12)
        controls.pack(side="left", fill="y")
        controls.pack_propagate(False)

        tk.Label(
            controls, text="카메라 · 조명 선택", bg="#f3f4f6",
            font=("Arial", 16, "bold"),
        ).pack(anchor="w", pady=(0, 10))

        camera_box = tk.LabelFrame(controls, text="1. 볼 카메라 선택", padx=8, pady=6, bg="#f3f4f6")
        camera_box.pack(fill="x", pady=(0, 10))
        for index, (name, device) in enumerate(CAMERAS):
            tk.Radiobutton(
                camera_box, text=f"{name}   {device}", variable=self.camera_var,
                value=index, command=self._start_selected_camera,
                bg="#f3f4f6", anchor="w",
            ).pack(fill="x", pady=3)

        light_box = tk.LabelFrame(controls, text="2. 켤 조명 선택", padx=8, pady=6, bg="#f3f4f6")
        light_box.pack(fill="x", pady=(0, 10))
        for index, (name, _, _) in enumerate(LIGHTS):
            tk.Radiobutton(
                light_box, text=name, variable=self.light_var, value=index,
                anchor="w", bg="#f3f4f6", indicatoron=False,
                selectcolor="#bfdbfe", relief="raised", height=2,
            ).pack(fill="x", pady=2)

        setting_box = tk.LabelFrame(controls, text="3. 밝기와 대기", padx=8, pady=6, bg="#f3f4f6")
        setting_box.pack(fill="x", pady=(0, 10))
        tk.Scale(
            setting_box, label=f"밝기 (안전 상한 {LIGHT_MAX}, 캘리브 기준 {LIGHT_BRIGHTNESS})",
            from_=0, to=LIGHT_MAX, orient="horizontal",
            variable=self.brightness_var, length=250, bg="#f3f4f6",
        ).pack(fill="x")
        wait_row = tk.Frame(setting_box, bg="#f3f4f6")
        wait_row.pack(fill="x")
        tk.Label(wait_row, text="조명 후 촬영 대기(초)", bg="#f3f4f6").pack(side="left")
        tk.Entry(wait_row, textvariable=self.settle_var, width=6).pack(side="right")

        action_box = tk.LabelFrame(controls, text="4. 조명·촬영", padx=8, pady=8, bg="#f3f4f6")
        action_box.pack(fill="x", pady=(0, 10))
        on_button = tk.Button(
            action_box, text="선택 조명 켜기", bg="#fde68a",
            command=self.turn_selected_light_on,
        )
        on_button.pack(fill="x", pady=3)
        off_button = tk.Button(
            action_box, text="선택 조명 끄기", bg="#e5e7eb",
            command=self.turn_selected_light_off,
        )
        off_button.pack(fill="x", pady=3)
        all_off_button = tk.Button(
            action_box, text="전체 조명 즉시 끄기", bg="#fecaca",
            command=self.turn_lights_off,
        )
        all_off_button.pack(fill="x", pady=3)
        capture_button = tk.Button(
            action_box, text="선택 조명 켜고 촬영", bg="#bfdbfe",
            font=("Arial", 11, "bold"), height=2,
            command=self.capture_with_selected_light,
        )
        capture_button.pack(fill="x", pady=(7, 3))
        self.buttons.extend((on_button, off_button, all_off_button, capture_button))

        port_box = tk.LabelFrame(controls, text="연결 설정", padx=8, pady=6, bg="#f3f4f6")
        port_box.pack(fill="x")
        tk.Label(port_box, text="라즈베리파이", bg="#f3f4f6").grid(row=0, column=0, sticky="w")
        tk.Entry(port_box, textvariable=self.host_var, width=17).grid(row=0, column=1, padx=5)
        tk.Label(port_box, text="서버 포트", bg="#f3f4f6").grid(row=1, column=0, sticky="w")
        tk.Entry(port_box, textvariable=self.port_var, width=17).grid(row=1, column=1, padx=5, pady=3)

        viewer = tk.Frame(body, bg="#171717", padx=12, pady=12)
        viewer.pack(side="right", fill="both", expand=True)
        header = tk.Frame(viewer, bg="#171717")
        header.pack(fill="x", pady=(0, 8))
        tk.Label(
            header, textvariable=self.status_var, bg="#171717", fg="white",
            font=("Arial", 12, "bold"), anchor="w",
        ).pack(fill="x")
        self.metric_label = tk.Label(
            header, textvariable=self.metric_var, bg="#171717", fg="#bbb", anchor="w",
        )
        self.metric_label.pack(fill="x", pady=(3, 0))
        self.preview = tk.Label(
            viewer, text="카메라 영상 대기 중...", bg="black", fg="white",
            font=("Arial", 14),
        )
        self.preview.pack(fill="both", expand=True)

    def _start_selected_camera(self) -> None:
        index = self.camera_var.get()
        name, device = CAMERAS[index]
        self.light_var.set(CAMERA_LIGHT_SELECTION[index])
        self.status_var.set(f"{name} 연결 중 · {device}")
        self.metric_var.set("밝기 측정값: -")
        with self.frame_lock:
            self.last_frame = None
            self.last_frame_at = 0.0
        try:
            self.live.start(device)
        except Exception as exc:
            self.status_var.set(f"카메라 연결 실패: {exc}")

    def _poll_frames(self) -> None:
        try:
            frame = None
            while True:
                try:
                    frame = self.live.frames.get_nowait()
                except queue.Empty:
                    break
            if frame is not None:
                with self.frame_lock:
                    self.last_frame = frame
                    self.last_frame_at = time.monotonic()
                self.preview_image = tk.PhotoImage(data=base64.b64encode(frame))
                self.preview.config(image=self.preview_image, text="")
                name, device = CAMERAS[self.camera_var.get()]
                if not self.busy:
                    self.status_var.set(f"실시간 보기 · {name} · {device}")
            elif self.live.last_error:
                self.status_var.set(f"카메라 오류: {self.live.last_error}")
                self.live.last_error = ""
        finally:
            self.root.after(50, self._poll_frames)

    def _controller(self) -> LightController:
        try:
            port = int(self.port_var.get())
        except ValueError as exc:
            raise ValueError("서버 포트는 숫자로 입력하세요.") from exc
        host = self.host_var.get().strip()
        if not host or port <= 0 or port > 65535:
            raise ValueError("올바른 라즈베리파이 주소와 서버 포트를 입력하세요.")
        if self.light is None or self.light.host != host or self.light.port != port:
            if self.light is not None:
                self.light.close()
            self.light = LightController(host, port)
        return self.light

    def _selected_light(self) -> tuple[LightController, str, str, str]:
        light = self._controller()
        index = self.light_var.get()
        name, on_template, off_command = LIGHTS[index]
        on_command = on_template.format(brightness=self.brightness_var.get())
        return light, name, on_command, off_command

    def turn_selected_light_on(self) -> None:
        try:
            light, name, command, _ = self._selected_light()
        except ValueError as exc:
            messagebox.showerror("조명 설정 오류", str(exc))
            return

        def worker() -> None:
            try:
                self._set_status(f"{name} 켜는 중 · {command}")
                light.send(command)
                self._set_status(f"{name} 켜짐 · 실시간 화면에서 확인하세요.")
            except Exception as exc:
                self._show_error("조명 오류", exc)

        threading.Thread(target=worker, daemon=True).start()

    def turn_selected_light_off(self) -> None:
        try:
            light, name, _, off_command = self._selected_light()
        except ValueError as exc:
            messagebox.showerror("조명 설정 오류", str(exc))
            return

        def worker() -> None:
            try:
                light.send(off_command)
                self._set_status(f"{name} 꺼짐")
            except Exception as exc:
                self._show_error("조명 오류", exc)

        threading.Thread(target=worker, daemon=True).start()

    def turn_lights_off(self) -> None:
        try:
            light = self._controller()
        except ValueError as exc:
            messagebox.showerror("조명 설정 오류", str(exc))
            return

        def worker() -> None:
            try:
                light.send("OFF")
                self._set_status("전체 조명을 껐습니다.")
            except Exception as exc:
                self._show_error("조명 오류", exc)

        threading.Thread(target=worker, daemon=True).start()

    def capture_with_selected_light(self) -> None:
        if self.busy:
            return
        try:
            light, light_name, command, off_command = self._selected_light()
            settle = float(self.settle_var.get())
            if settle < 0 or settle > 10:
                raise ValueError("촬영 대기 시간은 0~10초로 입력하세요.")
        except ValueError as exc:
            messagebox.showerror("설정 오류", str(exc))
            return
        if not self.live.running:
            messagebox.showerror("카메라 오류", "실시간 카메라가 연결되지 않았습니다.")
            return

        self.busy = True
        self._set_buttons(False)
        camera_index = self.camera_var.get()
        camera_name, _ = CAMERAS[camera_index]

        def worker() -> None:
            try:
                self._set_status(f"{light_name} 켜는 중 · {command}")
                light.send(command)
                target_time = time.monotonic() + settle
                self._set_status(f"{camera_name}: 조명 안정화 {settle:g}초 대기 중...")
                deadline = target_time + 3.0
                frame = None
                while time.monotonic() < deadline:
                    with self.frame_lock:
                        if self.last_frame is not None and self.last_frame_at >= target_time:
                            frame = self.last_frame
                            break
                    time.sleep(0.05)
                if frame is None:
                    raise RuntimeError("조명 점등 후 새 카메라 프레임을 받지 못했습니다.")

                CAPTURE_DIR.mkdir(exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                output = CAPTURE_DIR / f"camera{camera_index + 1}_{timestamp}.png"
                output.write_bytes(frame)
                metrics = measure_brightness(output)
                label, color = brightness_label(metrics["YAVG"])
                self.root.after(0, self._show_capture_result, output, metrics, label, color)
            except Exception as exc:
                self._show_error("촬영 오류", exc)
            finally:
                try:
                    light.send(off_command)
                except Exception:
                    pass
                self.root.after(0, self._finish_action)

        threading.Thread(target=worker, daemon=True).start()

    def _show_capture_result(
        self, output: Path, metrics: dict[str, float], label: str, color: str,
    ) -> None:
        self.status_var.set(f"촬영 완료 · {output.name} · 선택 조명 OFF")
        self.metric_var.set(
            f"{label} · 평균 {metrics['YAVG']:.1f} / 암부 {metrics['YLOW']:.1f} / 명부 {metrics['YHIGH']:.1f}"
        )
        self.metric_label.config(fg=color)

    def _set_status(self, text: str) -> None:
        self.root.after(0, self.status_var.set, text)

    def _show_error(self, title: str, error: Exception) -> None:
        self._set_status(f"{title}: {error}")
        self.root.after(0, messagebox.showerror, title, str(error))

    def _set_buttons(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in self.buttons:
            button.config(state=state)

    def _finish_action(self) -> None:
        self.busy = False
        self._set_buttons(True)

    def _on_close(self) -> None:
        self.live.stop()
        if self.light is not None:
            try:
                self.light.close()
            except OSError:
                pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    CameraLightUi().run()
