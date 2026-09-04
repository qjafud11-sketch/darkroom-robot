"""재부팅 후에도 같은 USB 자리를 가리키도록 포트를 고른다.

ttyACM / ttyUSB / video 번호는 꽂은 순서라서 매번 바뀐다.
시리얼은 by-id, 카메라는 허브 포트 + video-index0(영상)을 쓴다.
index1은 메타데이터라 화면이 안 나온다.
"""
from pathlib import Path
import re

# 최신 스킬셋 보드(시리얼 번호 있는 CH340)를 먼저 보고, 이 PC에 있는 기존 이름도 받는다.
ARM_BY_IDS = (
    "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B61033773-if00",
    "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0",
)
ARM_BY_ID = ARM_BY_IDS[0]
LIGHT_BY_ID = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A5069RR4-if00-port0"
SERVO_BY_PATH = "/dev/serial/by-path/pci-0000:80:14.0-usb-0:2.2:1.0-port0"
# 조명·서보가 아닌 여분 CH340 (허브 5.2). 아두이노 후보에서 뺀다.
EXTRA_CH340_BY_PATH = "/dev/serial/by-path/pci-0000:80:14.0-usb-0:5.2:1.0-port0"
LIGHT_FALLBACK = "/dev/ttyUSB1"

# 물리 자리가 바뀌지 않으면 번호도 유지. 나머지는 빈 번호부터 채운다.
PREFERRED_USB = {
    "2.4.1": 1,
    "2.4.2": 2,
    "2.4.3": 3,
    "2.3": 4,
}

USB_RE = re.compile(r"usb-0:([^:]+):")


def _existing(candidates):
    seen = []
    for candidate in candidates:
        path = Path(candidate)
        if not path.exists():
            continue
        resolved = str(path.resolve())
        if resolved not in seen:
            seen.append(resolved)
    return seen


def resolve_arm_port():
    skip = set()
    if Path(LIGHT_BY_ID).exists():
        skip.add(str(Path(LIGHT_BY_ID).resolve()))
    if Path(SERVO_BY_PATH).exists():
        skip.add(str(Path(SERVO_BY_PATH).resolve()))
    found = [path for path in _existing(ARM_BY_IDS) if path not in skip]
    if found:
        return found[0]
    acms = sorted(Path("/dev").glob("ttyACM*"), key=lambda p: p.name)
    if acms:
        return str(acms[0])
    return ARM_BY_IDS[0]


def resolve_light_port():
    found = _existing((LIGHT_BY_ID, LIGHT_FALLBACK))
    return found[0] if found else LIGHT_BY_ID


def resolve_servo_candidates():
    light = None
    if Path(LIGHT_BY_ID).exists():
        light = str(Path(LIGHT_BY_ID).resolve())
    extra = None
    if Path(EXTRA_CH340_BY_PATH).exists():
        extra = str(Path(EXTRA_CH340_BY_PATH).resolve())
    seen = []
    for candidate in (SERVO_BY_PATH, *sorted(Path("/dev").glob("ttyUSB*"), key=lambda p: p.name)):
        path = Path(candidate)
        if not path.exists():
            continue
        resolved = str(path.resolve())
        if resolved in seen or resolved == light or resolved == extra:
            continue
        seen.append(resolved)
    return seen


def _iter_capture_nodes():
    folder = Path("/dev/v4l/by-path")
    if not folder.exists():
        return
    seen = set()
    for path in sorted(folder.iterdir(), key=lambda p: p.name):
        name = path.name
        if "usbv2" in name or not name.endswith("-video-index0"):
            continue
        match = USB_RE.search(name)
        if not match:
            continue
        resolved = str(path.resolve())
        if not Path(resolved).exists() or resolved in seen:
            continue
        seen.add(resolved)
        yield match.group(1), resolved


def camera_slots():
    """캡처 노드를 카메라 1~4에 붙인다. 메타데이터 노드는 제외."""
    slots = {1: None, 2: None, 3: None, 4: None}
    leftovers = []
    for usb, device in _iter_capture_nodes():
        cam_id = PREFERRED_USB.get(usb)
        if cam_id and slots[cam_id] is None:
            slots[cam_id] = device
        else:
            leftovers.append(device)
    for cam_id in (1, 2, 3, 4):
        if slots[cam_id] is None and leftovers:
            slots[cam_id] = leftovers.pop(0)
    return [
        {"id": cam_id, "name": f"카메라 {cam_id}", "device": slots[cam_id]}
        for cam_id in (1, 2, 3, 4)
    ]


def report(log=print):
    arm = resolve_arm_port()
    light = resolve_light_port()
    servos = resolve_servo_candidates()
    log(f"[포트] SO-ARM  {arm}  {'있음' if Path(arm).exists() else '없음'}")
    log(f"[포트] 조명    {light}  {'있음' if Path(light).exists() else '없음'}")
    log(f"[포트] 서보    {servos[0] if servos else '없음'}")
    for cam in camera_slots():
        device = cam["device"] or "미연결"
        ok = device != "미연결" and Path(device).exists()
        log(f"[포트] {cam['name']}  {device}  {'있음' if ok else '없음'}")
