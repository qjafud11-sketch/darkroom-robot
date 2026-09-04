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


FOV_SHAPES = ("rect", "ellipse", "circle")
FOV_SHAPE_LABELS = {
    "rect": "직사각형",
    "ellipse": "타원",
    "circle": "원",
}
FOV_DEFAULTS = {
    "shape": "rect",
    "x": 0.0,
    "y": 0.0,
    "w": 1.0,
    "h": 1.0,
}
FOV_MIN_PX = 32
FOV_FRAME = (1280, 720)


def _clamp_float(value, lo, hi, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def _legacy_zoom_box(src):
    """예전 zoom/cx/cy 저장값을 x,y,w,h 로 바꾼다."""
    zoom = _clamp_float(src.get("zoom"), 0.0, 400.0, 1.0)
    if zoom > 4.01:
        zoom = zoom / 100.0
    zoom = _clamp_float(zoom, 1.0, 4.0, 1.0)
    size = 1.0 / zoom
    cx = _clamp_float(src.get("cx"), 0.0, 1.0, 0.5)
    cy = _clamp_float(src.get("cy"), 0.0, 1.0, 0.5)
    return {
        "shape": "rect",
        "x": cx - size / 2.0,
        "y": cy - size / 2.0,
        "w": size,
        "h": size,
    }


def normalize_fov(values=None, width=None, height=None):
    """화각. shape 는 rect/ellipse/circle, x,y,w,h 는 화면 비율(0~1)."""
    src = dict(values or {})
    width = int(width or FOV_FRAME[0])
    height = int(height or FOV_FRAME[1])
    if "w" not in src and "zoom" in src:
        src = _legacy_zoom_box(src)
    shape = str(src.get("shape") or "rect").lower()
    if shape not in FOV_SHAPES:
        shape = "rect"
    min_w = FOV_MIN_PX / max(width, 1)
    min_h = FOV_MIN_PX / max(height, 1)
    w = _clamp_float(src.get("w"), min_w, 1.0, 1.0)
    h = _clamp_float(src.get("h"), min_h, 1.0, 1.0)
    cx = _clamp_float(src.get("x"), 0.0, 1.0, 0.0) + w / 2.0
    cy = _clamp_float(src.get("y"), 0.0, 1.0, 0.0) + h / 2.0
    if shape == "circle":
        side = min(w * width, h * height, width, height)
        side = max(float(FOV_MIN_PX), side)
        w = side / width
        h = side / height
    x = _clamp_float(cx - w / 2.0, 0.0, max(0.0, 1.0 - w), 0.0)
    y = _clamp_float(cy - h / 2.0, 0.0, max(0.0, 1.0 - h), 0.0)
    return {"shape": shape, "x": x, "y": y, "w": w, "h": h}


def fov_active(fov):
    values = normalize_fov(fov)
    if values["shape"] != "rect":
        return True
    return values["w"] < 0.999 or values["h"] < 0.999 or values["x"] > 0.001 or values["y"] > 0.001


def fov_box(width, height, fov):
    """원본 크기에서 화각 상자 (x0, y0, x1, y1) 포함 좌표."""
    width, height = int(width), int(height)
    values = normalize_fov(fov, width, height)
    if width < 2 or height < 2:
        return (0, 0, max(0, width - 1), max(0, height - 1))
    x0 = int(round(values["x"] * width))
    y0 = int(round(values["y"] * height))
    crop_w = max(FOV_MIN_PX, min(width, int(round(values["w"] * width))))
    crop_h = max(FOV_MIN_PX, min(height, int(round(values["h"] * height))))
    x0 = max(0, min(width - crop_w, x0))
    y0 = max(0, min(height - crop_h, y0))
    return (x0, y0, x0 + crop_w - 1, y0 + crop_h - 1)


def fov_from_box(width, height, box, shape="rect"):
    x0, y0, x1, y1 = box
    width, height = max(int(width), 1), max(int(height), 1)
    return normalize_fov(
        {
            "shape": shape,
            "x": x0 / width,
            "y": y0 / height,
            "w": (x1 - x0 + 1) / width,
            "h": (y1 - y0 + 1) / height,
        },
        width,
        height,
    )


def _mask_ellipse(image):
    from PIL import ImageDraw

    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, image.size[0] - 1, image.size[1] - 1), fill=255)
    out = Image.new("RGB", image.size, (0, 0, 0))
    out.paste(image, mask=mask)
    return out


def _fit_canvas(image, width, height):
    """비율을 유지한 채 캔버스에 넣는다. 빈 칸은 검정."""
    src_w, src_h = image.size
    if src_w <= 0 or src_h <= 0:
        return Image.new("RGB", (width, height), (0, 0, 0))
    scale = min(width / src_w, height / src_h)
    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    resized = image.resize((new_w, new_h), Image.BICUBIC)
    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    canvas.paste(resized, ((width - new_w) // 2, (height - new_h) // 2))
    return canvas


def inspect_stage(label) -> int:
    """1차/2차 라벨 → 1 또는 2."""
    text = str(label or "").strip()
    if text.startswith("2"):
        return 2
    try:
        if int(text) == 2:
            return 2
    except ValueError:
        pass
    return 1


def fov_face_key(cam_id, stage=1) -> str | None:
    from dataset_label import FACES

    stage = 2 if int(stage or 1) == 2 else 1
    cam_id = int(cam_id)
    for face in FACES:
        if int(face["cam"]) == cam_id and int(face["stage"]) == stage:
            return face["key"]
    return None


def apply_fov(image, fov):
    """디지털 화각. 직사각형·타원·원. 비율은 유지하고 빈 칸은 검게 둔다."""
    out = image.convert("RGB")
    width, height = out.size
    values = normalize_fov(fov, width, height)
    if not fov_active(values):
        return out
    x0, y0, x1, y1 = fov_box(width, height, values)
    cropped = out.crop((x0, y0, x1 + 1, y1 + 1))
    if values["shape"] in ("ellipse", "circle"):
        cropped = _mask_ellipse(cropped)
    if cropped.size == (width, height) and values["shape"] == "rect":
        return cropped
    return _fit_canvas(cropped, width, height)


FOV_COMMENT_PREFIX = b"DRFOV:"


def fov_fingerprint(cam_id, fov=None, stage=1):
    payload = {
        "id": int(cam_id),
        "stage": int(stage or 1),
        "fov": normalize_fov(fov if fov is not None else load_fov(cam_id, stage=stage)),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _fov_comment_bytes(cam_id, fov=None, stage=1):
    return FOV_COMMENT_PREFIX + fov_fingerprint(cam_id, fov, stage=stage).encode("utf-8")


def _parse_fov_comment(raw):
    if not raw:
        return None
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    if not raw.startswith(FOV_COMMENT_PREFIX):
        return None
    try:
        return json.loads(raw[len(FOV_COMMENT_PREFIX) :].decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None


def _jpeg_comment(image):
    return image.info.get("comment") if hasattr(image, "info") else None


def _save_jpeg(image, path, comment=None):
    extra = {}
    if comment:
        extra["comment"] = comment if isinstance(comment, (bytes, bytearray)) else str(comment).encode("utf-8")
    image.convert("RGB").save(path, quality=95, subsampling=0, **extra)


def fov_already_applied(image, cam_id, fov=None, stage=1):
    """이 JPEG 에 지금 화각이 이미 들어가 있으면 True. 다시 자르지 않는다."""
    tagged = _parse_fov_comment(_jpeg_comment(image))
    if not tagged:
        return False
    want = json.loads(fov_fingerprint(cam_id, fov, stage=stage))
    return tagged.get("fov") == want["fov"] and int(tagged.get("id") or 0) == int(cam_id)


def open_camera_rgb(path, cam_id, stage=1):
    """학습·판정·미리보기가 같은 화각을 보도록 연다. 이미 넣은 사진은 다시 안 자른다.

    샘플 색이 있으면 고정 화각으로 자르지 않고 원본을 돌려준다.
    크롭은 sample_roi 가 매 장 테두리를 따라 한다.
    """
    source = Path(path)
    image = Image.open(source)
    fov = load_fov(cam_id, stage=stage)
    tagged = _parse_fov_comment(_jpeg_comment(image))
    rgb = image.convert("RGB")
    if tagged:
        return rgb
    if load_sample_color(cam_id, stage=stage):
        return rgb
    if not fov_active(fov):
        return rgb
    return apply_fov(rgb, fov)


def load_fov(cam_id, stage=1):
    """면(카메라+차수) 화각. 면값이 없으면 카메라 공통값을 쓴다."""
    store = load_store()
    stage = inspect_stage(stage) if not isinstance(stage, int) else (2 if stage == 2 else 1)
    key = fov_face_key(cam_id, stage)
    if key:
        face_fov = ((store.get("faces") or {}).get(key) or {}).get("fov")
        if face_fov:
            return normalize_fov(face_fov)
    item = store.get("cameras", {}).get(str(cam_id), {})
    return normalize_fov(item.get("fov"))


def save_fov(cam_id, fov, device="", name="", stage=1):
    store = load_store()
    cameras = store.setdefault("cameras", {})
    item = cameras.setdefault(str(cam_id), {})
    if device:
        item["device"] = device
    if name:
        item["name"] = name
    values = normalize_fov(fov)
    stage = 2 if int(stage or 1) == 2 else 1
    key = fov_face_key(cam_id, stage)
    if key:
        faces = store.setdefault("faces", {})
        slot = faces.setdefault(key, {})
        slot["fov"] = values
        slot["cam"] = int(cam_id)
        slot["stage"] = stage
    if stage == 1:
        item["fov"] = values
    return _write_store(store)


def _clamp_byte(value, default=0):
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(0, min(255, number))


def normalize_sample_color(raw):
    """면마다 찍은 샘플 HSV. picked 가 아니면 아직 없는 것으로 본다."""
    if not raw or not isinstance(raw, dict):
        return None
    if raw.get("picked") is False:
        return None
    if "h" not in raw or "s" not in raw or "v" not in raw:
        return None
    h = _clamp_byte(raw.get("h"), 40)
    s = _clamp_byte(raw.get("s"), 40)
    v = _clamp_byte(raw.get("v"), 170)
    return {
        "picked": True,
        "h": h,
        "s": s,
        "v": v,
        "h_tol": _clamp_byte(raw.get("h_tol"), 22),
        "s_tol": _clamp_byte(raw.get("s_tol"), 50),
        "v_tol": _clamp_byte(raw.get("v_tol"), 70),
    }


def load_sample_color(cam_id, stage=1):
    store = load_store()
    stage = 2 if int(stage or 1) == 2 else 1
    key = fov_face_key(cam_id, stage)
    if not key:
        return None
    raw = ((store.get("faces") or {}).get(key) or {}).get("sample_color")
    return normalize_sample_color(raw)


def save_sample_color(cam_id, color, stage=1):
    store = load_store()
    stage = 2 if int(stage or 1) == 2 else 1
    key = fov_face_key(cam_id, stage)
    if not key:
        return CALIB_PATH
    faces = store.setdefault("faces", {})
    slot = faces.setdefault(key, {})
    slot["cam"] = int(cam_id)
    slot["stage"] = stage
    values = normalize_sample_color(color)
    if values:
        slot["sample_color"] = values
    else:
        slot.pop("sample_color", None)
    return _write_store(store)


def image_has_fov_tag(path_or_image) -> bool:
    if isinstance(path_or_image, (str, Path)):
        image = Image.open(path_or_image)
    else:
        image = path_or_image
    return _parse_fov_comment(_jpeg_comment(image)) is not None


def apply_fov_in_place(cam_id, path, stage=1):
    """촬영 JPEG 에 저장된 면 화각을 바로 넣는다. 이미 넣은 사진은 건너뛴다.

    샘플 색 추적이 켜진 면은 원본 전체를 남겨 둔다. 위치가 조금 바뀌어도
    나중에 색으로 테두리를 다시 잡기 위해서다.
    """
    stage = 2 if int(stage or 1) == 2 else 1
    if load_sample_color(cam_id, stage=stage):
        return False
    fov = load_fov(cam_id, stage=stage)
    source = Path(path)
    image = Image.open(source)
    if _parse_fov_comment(_jpeg_comment(image)):
        return False
    if not fov_active(fov):
        return False
    rgb = apply_fov(image.convert("RGB"), fov)
    _save_jpeg(rgb, source, _fov_comment_bytes(cam_id, fov, stage=stage))
    return True


def save_camera(cam_id, device, values, name="", filters=None):
    store = load_store()
    cameras = store.setdefault("cameras", {})
    prev = cameras.get(str(cam_id), {})
    cameras[str(cam_id)] = {
        "device": device,
        "name": name,
        "controls": {key: int(val) for key, val in values.items()},
        "filters": normalize_filters(filters),
        "fov": normalize_fov(prev.get("fov")),
    }
    return _write_store(store)


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
    source = Path(path)
    image = Image.open(source)
    comment = _jpeg_comment(image)
    rgb = apply_filters(image.convert("RGB"), filters)
    _save_jpeg(rgb, source, comment)
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
    image = Image.open(source)
    comment = _jpeg_comment(image)
    rgb = apply_filters(image.convert("RGB"), filters)
    _save_jpeg(rgb, dest, comment)
    return dest
