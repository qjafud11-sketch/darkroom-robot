import json
import socket

from hw_ports import report as report_ports
from pipeline import PIPELINE_COMMANDS, get_ui_snapshot, run_step

HOST = "127.0.0.1"  # 로컬 테스트 시 127.0.0.1, 실제 NUC가 다른 PC라면 해당 IP로 변경
PORT = 8585


def handle_command(command):
    """명령 하나를 실행하고 (상태, 메시지)를 돌려준다.

    한 단계가 끝나야 DONE을 보낸다. 다음 단계는 UI가 그 신호를 받은 뒤에 온다.
    """
    try:
        if command in PIPELINE_COMMANDS:
            return run_step(command)

        print(f" 알 수 없는 명령: {command}")
        return "ERROR", f"{command} 명령을 찾을 수 없음"

    except Exception as e:
        print(f" [오류] {command} 실행 실패: {e}")
        return "ERROR", f"{command} 실행 실패: {e}"


def run_client():
    print(f"[실행기] 서버({HOST}:{PORT})에 연결을 시도합니다...")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.connect((HOST, PORT))
            print("[실행기] 서버에 연결 성공. 명령을 대기합니다.\n")

            fd = s.makefile("r")

            while True:
                data = fd.readline()
                if not data:
                    print("[실행기] 서버가 닫혔습니다.")
                    break

                msg = json.loads(data)
                command = msg.get("command", "")
                print("-" * 40)
                print(f"[실행기 수신] 명령: {command}")

                status, reply_msg = handle_command(command)
                response = {
                    "status": status,
                    "message": reply_msg,
                    "command": command,
                }
                if status == "DONE":
                    response.update(get_ui_snapshot())
                print(f"[실행기 송신] {status} — {reply_msg}")
                s.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))

        except ConnectionRefusedError:
            print("[실행기] 연결 실패. gui_server.py가 켜져 있는지 확인하세요.")


if __name__ == "__main__":
    report_ports()
    run_client()
