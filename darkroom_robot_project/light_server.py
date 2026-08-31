"""라즈베리파이에서 실행하는 Arduino NeoPixel TCP 제어 서버."""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import socketserver
import termios
import threading
import time


class ArduinoLight:
    def __init__(self, device: str, baud: int):
        self.device = device
        self.baud = baud
        self.fd: int | None = None
        self.lock = threading.Lock()

    def _open(self) -> None:
        if self.fd is not None:
            return
        if self.baud != 9600:
            raise ValueError("현재 조명 서버는 9600 baud만 지원합니다.")
        fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY)
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0
        attrs[4] = termios.B9600
        attrs[5] = termios.B9600
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 10
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
        self.fd = fd
        time.sleep(2.0)

    def _readline(self, timeout: float = 1.0) -> str:
        assert self.fd is not None
        deadline = time.monotonic() + timeout
        data = bytearray()
        while time.monotonic() < deadline:
            readable, _, _ = select.select([self.fd], [], [], max(0, deadline - time.monotonic()))
            if not readable:
                break
            chunk = os.read(self.fd, 64)
            if not chunk:
                break
            data.extend(chunk)
            if b"\n" in data:
                break
        return data.decode("ascii", errors="replace").strip()

    def command(self, command: str) -> str:
        with self.lock:
            self._open()
            assert self.fd is not None
            termios.tcflush(self.fd, termios.TCIFLUSH)
            os.write(self.fd, (command.strip() + "\n").encode("ascii"))
            response = self._readline()
            if response.startswith("ERR"):
                raise RuntimeError(response)
            return response or "명령 전송 완료"

    def close(self) -> None:
        with self.lock:
            if self.fd is None:
                return
            try:
                os.write(self.fd, b"OFF\n")
            finally:
                os.close(self.fd)
                self.fd = None


class LightRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(4096)
        try:
            request = json.loads(line.decode("utf-8"))
            command = str(request.get("command", "")).strip()
            if not command:
                raise ValueError("빈 조명 명령")
            message = self.server.light.command(command)
            response = {"status": "OK", "message": message}
        except Exception as exc:
            response = {"status": "ERROR", "message": str(exc)}
        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


class LightServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, light: ArduinoLight):
        self.light = light
        super().__init__(address, handler)


from arduino_link import resolve_port


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Arduino NeoPixel 조명 TCP 서버")
    parser.add_argument("--device", default="", help="Arduino 시리얼. 비우면 FTDI 자동")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9001)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    device = args.device or resolve_port()
    light = ArduinoLight(device, args.baud)
    server = LightServer((args.host, args.port), LightRequestHandler, light)

    def stop_server(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop_server)
    signal.signal(signal.SIGTERM, stop_server)
    print(f"조명 서버 대기: {args.host}:{args.port} -> {device}@{args.baud}")
    try:
        server.serve_forever()
    finally:
        light.close()
        server.server_close()


if __name__ == "__main__":
    main()
