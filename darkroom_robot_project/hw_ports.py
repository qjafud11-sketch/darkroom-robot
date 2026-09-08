"""암실 하드웨어의 USB·카메라 포트를 역할별로 찾는다.

ttyACM / ttyUSB / video 번호는 꽂은 순서라서 매번 바뀐다.
시리얼은 by-id/by-path를 우선하고, 보드의 READY 배너로 최종 확인한다.
카메라는 허브 포트 + video-index0(영상)을 쓴다.
index1은 메타데이터라 화면이 안 나온다.
"""
from pathlib import Path
import re

# 최신 스킬셋 보드(시리얼 번호 있는 CH340)를 먼저 보고, 이 PC에 있는 기존 이름도 받는다.
# 이 PC 부팅 실측 (2026-09-04, pci-0000:00:14.0 허브 5.x)
#   5.1  SO-ARM101  CH340 ACM  ttyACM0
#   5.2  서보 아두이노 CH340   ttyUSB0
#   5.3  카메라 4
#   5.4.1~5.4.3  카메라 1~3
#   5.4.4  조명 아두이노 FTDI A5069RR4  ttyUSB1
ARM_BY_IDS = (
    "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B61033773-if00",
)
ARM_BY_ID = ARM_BY_IDS[0]
LIGHT_BY_ID = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A5069RR4-if00-port0"
SERVO_BY_ID = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"
SERVO_BY_PATH = "/dev/serial/by-path/pci-0000:00:14.0-usb-0:5.2:1.0-port0"
# 이 머신에는 여분 CH340이 없다. 없는 경로로 두면 후보에서 빼지 않는다.
EXTRA_CH340_BY_PATH = "/dev/serial/by-path/pci-0000:00:14.0-usb-0:9.9:1.0-port0"
LIGHT_FALLBACK = "/dev/ttyUSB1"

# 물리 자리가 바뀌지 않으면 번호도 유지. 나머지는 빈 번호부터 채운다.
PREFERRED_USB = {
    "5.4.1": 1,
    "5.4.2": 2,
    "5.4.3": 3,
    "5.3": 4,
}

USB_RE = re.compile(r"usb-0:([^:]+):")


def _serial_devices():
    """현재 존재하는 실제 시리얼 디바이스를 중복 없이 반환한다.

    tty 번호는 연결 순서에 따라 바뀌므로 이 함수의 결과를 역할에
    직접 대입하지 말고, arduino_link의 READY 배너 검증과 함께 사용한다.
    """
    paths = [*Path("/dev").glob("ttyACM*"), *Path("/dev").glob("ttyUSB*")]
    return _existing(sorted(paths, key=lambda path: path.name))


def _serial_links(folder):
    """by-id/by-path 링크를 이름 순서로 반환한다."""
    root = Path(folder)
    if not root.exists():
        return []
    return [path for path in sorted(root.iterdir(), key=lambda item: item.name) if path.is_symlink()]


def _named_serial_links(*tokens):
    """이름에 토큰이 들어간 by-id 링크의 실제 포트 목록."""
    wanted = tuple(token.lower() for token in tokens)
    links = []
    for path in _serial_links("/dev/serial/by-id"):
        if any(token in path.name.lower() for token in wanted):
            links.append(str(path))
    return links


def light_candidate_ports():
    """조명 보드 후보. FTDI 식별자를 먼저, 번호 포트는 마지막에 둔다."""
    return _existing(
        (
            *light_identity_ports(),
            LIGHT_FALLBACK,
            *_serial_devices(),
        )
    )


def light_identity_ports():
    """FTDI/조명 보드로 식별되는 포트만 반환한다."""
    return _existing((LIGHT_BY_ID, *_named_serial_links("ftdi", "ft232", "a5069")))


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
    found = light_candidate_ports()
    return found[0] if found else LIGHT_BY_ID


def resolve_servo_candidates():
    light = set(light_identity_ports())
    extra = None
    if Path(EXTRA_CH340_BY_PATH).exists():
        extra = str(Path(EXTRA_CH340_BY_PATH).resolve())
    arm = _resolve_arm_for_exclusion()
    seen = []
    for candidate in (
        SERVO_BY_ID,
        SERVO_BY_PATH,
        *_named_serial_links("ch340", "usb_serial"),
        *_serial_devices(),
    ):
        path = Path(candidate)
        if not path.exists():
            continue
        resolved = str(path.resolve())
        if resolved in seen or resolved in light or resolved in {extra, arm}:
            continue
        seen.append(resolved)
    return seen


def _resolve_arm_for_exclusion():
    """서보 후보에서 ARM 포트를 빼기 위한 안전한 ARM 조회."""
    found = _existing(ARM_BY_IDS)
    if found:
        return found[0]
    acms = sorted(Path("/dev").glob("ttyACM*"), key=lambda path: path.name)
    return str(acms[0].resolve()) if acms else None


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


def extra_capture_nodes():
    """검사 슬롯 1~4에 안 들어간 캡처 노드. 실시간 프리뷰용."""
    extras = []
    for usb, device in _iter_capture_nodes():
        if usb in PREFERRED_USB:
            continue
        extras.append(
            {
                "usb": usb,
                "device": device,
                "name": f"추가 카메라  USB {usb}",
            }
        )
    return extras


def report(log=print):
    serials = _serial_devices()
    if serials:
        log("[포트] 시리얼 전체")
        for port in serials:
            log(f"[포트]   {port}")
    else:
        log("[포트] 시리얼 전체  없음")

    arm = resolve_arm_port()
    light = resolve_light_port()
    servos = resolve_servo_candidates()
    log(f"[포트] SO-ARM  {arm}  {'있음' if Path(arm).exists() else '없음'}")
    log(f"[포트] 조명    {light}  {'있음' if Path(light).exists() else '없음'}")
    log(f"[포트] 서보 후보  {', '.join(servos) if servos else '없음'}")
    for cam in camera_slots():
        device = cam["device"] or "미연결"
        ok = device != "미연결" and Path(device).exists()
        log(f"[포트] {cam['name']}  {device}  {'있음' if ok else '없음'}")


if __name__ == "__main__":
    import sys

    report()
    if "--ping-light" in sys.argv:
        from arduino_link import send_ok

        try:
            reply = send_ok("OFF", timeout=3.0, must_reply=True)
            print(f"[ping-light] {reply or 'FAIL'}")
        except Exception as exc:
            print(f"[ping-light] FAIL  {exc}")
