"""카메라 프리뷰·순간 캡처.

슬롯은 카메라 1~4. 프리뷰·캘리브는 한 대만 연다.
검사 파이프라인은 조명 OK 뒤에 capture()를 부른다.
UI가 켜져 있으면 UI가 장치를 잡고 있으므로 TCP로 촬영을 부탁한다.
UI가 없으면 이 파일이 ffmpeg로 직접 찍는다.
저장만 한다. AI 추론은 나중에 이 폴더를 읽으면 된다.
"""
from __future__ import annotations

import json
import queue
import socket
import socketserver
import struct
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from hw_ports import camera_slots

CAPTURE_DIR = Path.home() / "darkroom_captures"
CAM_HOST = "127.0.0.1"
CAM_PORT = 8586
USB_LOCK = threading.Lock()

# video 번호는 재부팅마다 바뀐다. 허브 포트의 캡처 노드(index0)를 고른다.
CAMERAS = camera_slots()


def connected_cameras():
    """장치가 있는 슬롯만."""
    out = []
    for cam in CAMERAS:
        device = cam.get("device")
        if device and Path(device).exists():
            out.append(cam)
    return out


def _read_exact(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise EOFError("카메라 스트림이 끊겼습니다.")
        chunks.extend(chunk)
    return bytes(chunks)


def read_png(stream):
    signature = _read_exact(stream, 8)
    if signature != b"\x89PNG\r\n\x1a\n":
        raise ValueError("PNG 프레임이 아닙니다.")
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
    """프리뷰용. 저장 촬영은 grab_stills가 고해상도로 따로 찍는다."""

    def __init__(self, width=320, height=240, fps=5):
        self.width = width
        self.height = height
        self.fps = fps
        self.frames = queue.Queue(maxsize=1)
        self.process = None
        self.thread = None
        self.device = ""
        self.last_error = ""
        self.running = False

    def start(self, device):
        self.stop()
        self.device = device
        self.last_error = ""
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", f"{self.width}x{self.height}", "-framerate", str(self.fps),
            "-i", device,
            "-vf", f"scale={self.width}:{self.height}",
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

    def _reader(self):
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

    def stop(self):
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


def _new_folder(label):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = CAPTURE_DIR / f"{label}_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _write_manifest(folder, label, files):
    cameras = []
    for cam in CAMERAS:
        item = {
            "id": cam["id"],
            "name": cam["name"],
            "device": cam["device"],
            "file": files.get(cam["id"]),
        }
        if not cam["device"]:
            item["note"] = "미연결"
        cameras.append(item)
    data = {
        "label": label,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "folder": str(folder),
        "cameras": cameras,
    }
    path = folder / "manifest.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def grab_preview_png(device, cam_id=None):
    """카메라 한 대만 열어 PNG 한 장을 받는다. 세 대를 동시에 열면 USB가 터진다."""
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "v4l2", "-input_format", "mjpeg",
        "-video_size", "320x240", "-framerate", "5",
        "-i", device,
        "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-",
    ]
    with USB_LOCK:
        if cam_id is not None:
            try:
                from camera_calib import apply_saved
                apply_saved(cam_id, device)
            except Exception:
                pass
        result = subprocess.run(command, capture_output=True, timeout=6)
    if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG"):
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "프리뷰 실패")
    return result.stdout


def grab_stills(label):
    """연결된 카메라를 한 장씩 고해상도로 찍는다. 프리뷰는 호출 전에 멈춰야 한다.

    USB 대역 때문에 세 대를 동시에 열지 않고 순서대로 찍는다.
    """
    folder = _new_folder(label)
    files = {}
    errors = []

    def _one(cam):
        from camera_calib import apply_saved, apply_saved_filters
        out = folder / f"cam{cam['id']}.jpg"
        try:
            apply_saved(cam["id"], cam["device"])
        except Exception:
            pass
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", "1280x720", "-framerate", "10",
            "-i", cam["device"],
            "-frames:v", "1", "-y", str(out),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=8)
        if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            detail = (result.stderr or result.stdout or "촬영 실패").strip().splitlines()
            raise RuntimeError(f"{cam['name']} 촬영 실패: {detail[-1] if detail else 'unknown'}")
        try:
            apply_saved_filters(cam["id"], out)
        except Exception:
            pass
        return cam["id"], str(out)

    with USB_LOCK:
        for cam in connected_cameras():
            try:
                cam_id, path = _one(cam)
                files[cam_id] = path
            except Exception as exc:
                errors.append(str(exc))

    if errors:
        raise RuntimeError(" / ".join(errors))
    if not files:
        raise RuntimeError("연결된 카메라가 없어 촬영하지 못했습니다.")
    manifest = _write_manifest(folder, label, files)
    print(f"[카메라] 저장 {folder}")
    for cam_id, path in sorted(files.items()):
        print(f"[카메라]   cam{cam_id} {path}")
    return manifest


def capture_via_ui(label):
    request = json.dumps({"command": "CAPTURE", "label": label}, ensure_ascii=False) + "\n"
    with socket.create_connection((CAM_HOST, CAM_PORT), timeout=20) as sock:
        sock.sendall(request.encode("utf-8"))
        line = sock.makefile("r", encoding="utf-8").readline()
    if not line:
        raise ConnectionError("UI 카메라 서버가 응답하지 않습니다.")
    response = json.loads(line)
    if response.get("status") != "OK":
        raise RuntimeError(response.get("message", "촬영 실패"))
    return response


def capture(label):
    """조명 ON(OK) 뒤에 호출. UI가 있으면 UI에 부탁, 없으면 직접 촬영."""
    print(f"[카메라] → CAPTURE {label}")
    try:
        response = capture_via_ui(label)
        folder = response.get("folder", "")
        print(f"[카메라] ← OK {label} (UI) {folder}")
        return "OK"
    except OSError:
        manifest = grab_stills(label)
        print(f"[카메라] ← OK {label} (직접) {manifest['folder']}")
        return "OK"


class _CaptureHandler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline(4096)
        try:
            request = json.loads(line.decode("utf-8"))
            if request.get("command") != "CAPTURE":
                raise ValueError("CAPTURE만 받습니다.")
            label = str(request.get("label") or "capture")
            manifest = self.server.grabber(label)
            response = {
                "status": "OK",
                "folder": manifest["folder"],
                "message": f"{label} 촬영 완료",
            }
        except Exception as exc:
            response = {"status": "ERROR", "message": str(exc)}
        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


class CaptureServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, grabber):
        self.grabber = grabber
        super().__init__(address, _CaptureHandler)


def start_capture_server(grabber, host="127.0.0.1", port=CAM_PORT):
    """UI 프로세스에서 호출. robot_client의 capture()가 여기로 붙는다."""
    server = CaptureServer((host, port), grabber)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[카메라] UI 촬영 서버 {host}:{port}")
    return server
