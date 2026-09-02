"""카메라별 V4L2 캘리브레이션 + 소프트웨어 보정.

값은 ~/darkroom_calib.json 에 카메라 id 별로 남긴다.
상황 바뀔 때마다 UI에서 다시 맞추면 된다.
지금 달린 C270은 고정 초점이라 초점 슬라이더가 없다.
대신 소프트웨어 선명·대비를 저장해 검사 사진 옆에 보정본(cam2_ai.jpg)으로 남긴다.
원본은 그대로 두므로, 보정값을 바꿔도 다시 찍지 않고 보정본만 다시 만들면 된다.

이 값들은 사람 눈이 아니라 검출기(YOLO 등) 기준으로 맞춘 것이다.
분리도를 숫자로 보려면 calib_score / calib_probe 를 쓴다.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageStat

CALIB_PATH = Path.home() / "darkroom_calib.json"

LABELS = {
    "brightness": "밝기",
    "contrast": "대비",
    "saturation": "채도",
    "sharpness": "선명도",
    "hue": "색조",
    "gamma": "감마",
    "gain": "게인",
    "backlight_compensation": "역광 보정",
    "white_balance_automatic": "화이트밸런스 자동",
    "white_balance_temperature": "색온도",
    "power_line_frequency": "전원 주파수",
    "auto_exposure": "노출 모드",
    "exposure_time_absolute": "노출 시간",
    "exposure_dynamic_framerate": "노출 가변 프레임",
    "focus_automatic_continuous": "자동 초점",
    "focus_absolute": "초점",
    "zoom_absolute": "줌",
}

GROUPS = (
    ("밝기 · 색", ("brightness", "contrast", "saturation", "sharpness", "hue", "gamma")),
    ("노출", ("auto_exposure", "exposure_time_absolute", "gain", "backlight_compensation", "exposure_dynamic_framerate")),
    ("화이트밸런스", ("white_balance_automatic", "white_balance_temperature", "power_line_frequency")),
    ("초점", ("focus_automatic_continuous", "focus_absolute", "zoom_absolute")),
)

AUTO_FIRST = (
    "white_balance_automatic",
    "auto_exposure",
    "focus_automatic_continuous",
)

CTRL_LINE = re.compile(
    r"^\s+(\S+)\s+0x[0-9a-fA-F]+\s+\((\w+)\)\s+:\s+(.*)$"
)
FIELD = re.compile(r"(\w+)=(\S+)")
MENU_LINE = re.compile(r"^\s+(\d+):\s+(.+)$")


def label_of(name):
    return LABELS.get(name, name)


def _run(args, timeout=4):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "v4l2-ctl 실패").strip()
        raise RuntimeError(err.splitlines()[-1] if err else "v4l2-ctl 실패")
    return result.stdout


def _to_int(token):
    token = token.split("(", 1)[0].rstrip(",")
    try:
        return int(token)
    except ValueError:
        return token


def list_controls(device):
    """장치가 실제로 가진 컨트롤. 없는 항목(초점 등)은 빠진다."""
    if not device or not Path(device).exists():
        return []
    raw = _run(["v4l2-ctl", "-d", device, "--list-ctrls-menus"])
    controls = []
    current = None
    for line in raw.splitlines():
        match = CTRL_LINE.match(line)
        if match:
            if current:
                controls.append(current)
            name, kind, rest = match.group(1), match.group(2), match.group(3)
            flags = []
            if "flags=" in rest:
                rest, flagpart = rest.split("flags=", 1)
                flags = [
                    part.strip()
                    for part in flagpart.split(",")
                    if part.strip() and not part.strip().startswith("0x")
                ]
            fields = {key: _to_int(val) for key, val in FIELD.findall(rest)}
            current = {
                "name": name,
                "type": kind,
                "min": fields.get("min", 0),
                "max": fields.get("max", 1),
                "step": fields.get("step", 1) or 1,
                "default": fields.get("default", fields.get("value", 0)),
                "value": fields.get("value", fields.get("default", 0)),
                "inactive": "inactive" in flags,
                "read_only": "read-only" in flags,
                "menus": {},
            }
            continue
        menu = MENU_LINE.match(line)
        if menu and current is not None:
            current["menus"][int(menu.group(1))] = menu.group(2).strip()
    if current:
        controls.append(current)
    return controls


def _set_payload(device, values):
    ordered = [name for name in AUTO_FIRST if name in values]
    ordered.extend(name for name in values if name not in ordered)
    payload = ",".join(f"{name}={int(values[name])}" for name in ordered)
    _run(["v4l2-ctl", "-d", device, f"--set-ctrl={payload}"])


def set_controls(device, values):
    """값을 장치에 쓴다. 자동 항목을 먼저 넣고, 비활성(수동 전용) 값은 건너뛴다."""
    if not device or not values:
        return
    auto = {name: values[name] for name in AUTO_FIRST if name in values}
    if auto:
        _set_payload(device, auto)
    live = {item["name"]: item for item in list_controls(device)}
    rest = {}
    for name, val in values.items():
        if name in auto:
            continue
        item = live.get(name)
        if item is None or item.get("inactive"):
            continue
        rest[name] = val
    if rest:
        _set_payload(device, rest)


def current_values(controls):
    return {item["name"]: item["value"] for item in controls}


def load_store():
    if not CALIB_PATH.exists():
        return {"cameras": {}}
    try:
        return json.loads(CALIB_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"cameras": {}}


FILTER_DEFAULTS = {
    "unsharp": 0,
    "unsharp_radius": 15,
    "local_contrast": 0,
    "denoise": 0,
}

# 흠집 검출용으로 실측하다 보니 예전 상한(선명 250, 범위 4.0px)이 모자랐다.
# 흠집 폭이 10px쯤이라 그만한 범위로 세게 밀어야 검출기 쪽 분리도가 올라간다.
FILTER_LIMITS = {
    "unsharp": 700,
    "unsharp_radius": 200,
    "local_contrast": 80,
    "denoise": 5,
}


def _clamp(value, lo, hi, default=0):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def normalize_filters(values=None):
    src = values or {}
    return {
        "unsharp": _clamp(src.get("unsharp"), 0, FILTER_LIMITS["unsharp"], 0),
        "unsharp_radius": _clamp(src.get("unsharp_radius"), 5, FILTER_LIMITS["unsharp_radius"], 15),
        "local_contrast": _clamp(src.get("local_contrast"), 0, FILTER_LIMITS["local_contrast"], 0),
        "denoise": _clamp(src.get("denoise"), 0, FILTER_LIMITS["denoise"], 0),
    }


def filters_active(filters):
    values = normalize_filters(filters)
    return any(values[key] for key in ("unsharp", "local_contrast", "denoise"))


def apply_filters(image, filters):
    """프리뷰·저장 사진에 같은 소프트웨어 보정을 넣는다."""
    values = normalize_filters(filters)
    if not filters_active(values):
        return image
    out = image.convert("RGB")
    denoise = values["denoise"]
    if denoise >= 4:
        out = out.filter(ImageFilter.MedianFilter(size=5))
    elif denoise >= 1:
        out = out.filter(ImageFilter.MedianFilter(size=3))
    if values["unsharp"] > 0:
        radius = values["unsharp_radius"] / 10.0
        out = out.filter(
            ImageFilter.UnsharpMask(
                radius=radius,
                percent=values["unsharp"],
                threshold=2,
            )
        )
    if values["local_contrast"] > 0:
        out = ImageEnhance.Contrast(out).enhance(1.0 + values["local_contrast"] / 50.0)
    return out


def sharpness_score(image):
    """가장자리 분산. 높을수록 또렷. 초점 대신 비교용."""
    gray = image.convert("L")
    if max(gray.size) > 640:
        gray = gray.copy()
        gray.thumbnail((640, 480))
    edges = gray.filter(
        ImageFilter.Kernel((3, 3), [-1, -1, -1, -1, 8, -1, -1, -1, -1], scale=1)
    )
    return float(ImageStat.Stat(edges).var[0])


def save_camera(cam_id, device, values, name="", filters=None):
    store = load_store()
    cameras = store.setdefault("cameras", {})
    cameras[str(cam_id)] = {
        "device": device,
        "name": name,
        "controls": {key: int(val) for key, val in values.items()},
        "filters": normalize_filters(filters),
    }
    CALIB_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    return CALIB_PATH


def load_camera(cam_id):
    store = load_store()
    item = store.get("cameras", {}).get(str(cam_id), {})
    return dict(item.get("controls") or {})


def _write_store(store):
    CALIB_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    return CALIB_PATH


def save_preset(name, note=""):
    """지금 저장된 네 대 캘리브를 프리셋으로 떠 둔다.

    기본값으로 되돌려 다시 맞출 때, 되돌리기 전 값을 잃지 않으려고 쓴다.
    프리셋은 같은 파일 안에 따로 들어가므로 cameras 를 덮어도 남는다.
    """
    store = load_store()
    presets = store.setdefault("presets", {})
    presets[str(name)] = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "note": note,
        "cameras": json.loads(json.dumps(store.get("cameras", {}))),
    }
    return _write_store(store)


def list_presets():
    store = load_store()
    return {
        key: {"saved_at": item.get("saved_at", ""), "note": item.get("note", "")}
        for key, item in (store.get("presets") or {}).items()
    }


def restore_preset(name):
    """프리셋을 현재 캘리브 자리로 되돌린다. 프리셋 자체는 남는다."""
    store = load_store()
    preset = (store.get("presets") or {}).get(str(name))
    if not preset:
        raise KeyError(f"프리셋 {name} 이 없습니다.")
    store["cameras"] = json.loads(json.dumps(preset.get("cameras", {})))
    _write_store(store)
    return sorted(store["cameras"])


def reset_saved_to_defaults(cam_id, device):
    """저장값을 장치 기본값 + 필터 끄기로 되돌리고 장치에도 넣는다.

    UI의 '장치 기본값' 버튼은 화면만 되돌리고 저장은 안 한다.
    여기서는 파일까지 바꿔서, 창을 다시 열어도 기본값으로 뜨게 한다.
    """
    if not device:
        return None
    live = list_controls(device)
    if not live:
        return None
    saved = load_camera(cam_id)
    # 예전에 저장했던 항목만 기본값으로 되돌린다. 초점·줌까지 끌어오면
    # 저장 파일 모양이 프리셋과 달라져 나중에 비교하기 나쁘다.
    names = set(saved) or {item["name"] for item in live}
    defaults = {
        item["name"]: item["default"]
        for item in live
        if item["name"] in names and not item.get("read_only")
    }
    stored = load_store().get("cameras", {}).get(str(cam_id), {})
    save_camera(cam_id, device, defaults, stored.get("name", ""), FILTER_DEFAULTS)
    set_controls(device, defaults)
    return defaults


def load_filters(cam_id):
    store = load_store()
    item = store.get("cameras", {}).get(str(cam_id), {})
    return normalize_filters(item.get("filters"))


def apply_saved(cam_id, device):
    """저장된 값이 있으면 장치에 넣는다. 검사 촬영 직전에 호출."""
    values = load_camera(cam_id)
    if not values or not device:
        return False
    set_controls(device, values)
    return True


def apply_defaults(device):
    """드라이버 기본값으로 되돌린다. 운영 UI 프리뷰가 이 상태로 보인다.

    캘리브 값은 검출기용이라 사람이 보기엔 어둡고 흑백이다.
    그래서 화면은 기본값(자동 노출·자동 화이트밸런스)으로 두고,
    촬영 직전에만 apply_saved 로 바꿔 찍는다.
    """
    if not device:
        return False
    values = {
        item["name"]: item["default"]
        for item in list_controls(device)
        if not item.get("read_only") and isinstance(item.get("default"), int)
    }
    if not values:
        return False
    set_controls(device, values)
    return True


def ai_path(path):
    source = Path(path)
    return source.with_name(f"{source.stem}_ai{source.suffix}")


def save_filtered_in_place(cam_id, path):
    """보정본 한 장만 남긴다. 원본 자리에 덮어쓴다.

    어노테이션용 데이터셋은 판독 때와 같은 그림 한 장이면 된다.
    원본까지 남기면 장수가 두 배가 되고 어느 쪽에 상자를 쳤는지 헷갈린다.
    검사 촬영은 반대로 원본을 남겨야 하니 save_ai_copy 를 쓴다.
    """
    filters = load_filters(cam_id)
    if not filters_active(filters):
        return False
    image = Image.open(path).convert("RGB")
    apply_filters(image, filters).save(path, quality=95, subsampling=0)
    return True


def save_ai_copy(cam_id, path):
    """보정본을 옆에 따로 남긴다. 원본은 건드리지 않는다.

    예전에는 원본 JPEG을 덮어썼는데, 보정값을 바꾸면 다시 찍어야 했다.
    학습·어노테이션은 보정본을, 재보정은 원본을 쓰면 된다.
    """
    filters = load_filters(cam_id)
    if not filters_active(filters):
        return None
    source = Path(path)
    dest = ai_path(source)
    image = Image.open(source).convert("RGB")
    apply_filters(image, filters).save(dest, quality=95, subsampling=0)
    return dest
