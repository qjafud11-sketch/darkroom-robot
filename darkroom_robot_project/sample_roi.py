"""검사 프레임에서 샘플만 잘라 낸다.

전체 1280×720 을 PatchCore 에 넣으면 케이블·링조명 반사·지그 그림자가
조금만 바뀌어도 이상으로 잡힌다. 학습·채점 모두 이 크롭을 쓴다.

면마다 찍은 샘플 색이 있으면 그 HSV 로 테두리를 따라간다. 위치가 조금
밀려도 같은 물체만 남긴다. 스크래치·찌그러짐은 색이 달라도 안쪽 구멍으로
빼지 않는다 — 닫힘·구멍 메움·볼록 외곽으로 네모 안을 통째로 남긴다.

색이 없으면 예전처럼 밝은 회색 덩어리를 찾는다. 크롭은 가로·세로 상자만
쓰지 않는다. 기울기를 구해 샘플 변에 맞춰 자른 뒤 그 조각을 세운다.

  python sample_roi.py --preview /tmp/roi
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter
import numpy as np

from dataset_label import FACES, UNSUP_DIR, shot_path

ROI_PATH = UNSUP_DIR / "roi.json"
MODELS_ROI = Path.home() / "darkroom_models" / "unsup" / "roi.json"

# 크롭이 너무 작거나 실패하면 이 중앙 윈도우로 떨어진다.
_FALLBACK = (0.20, 0.05, 0.80, 0.78)
# 이보다 작은 기울기는 축정렬 상자로 둔다 (불필요한 재샘플 방지).
_MIN_TILT = 2.5


def _proj_span(values: np.ndarray, min_frac: float, smooth_div: int = 40) -> tuple[int, int]:
    v = values.astype(float)
    if v.size == 0 or float(v.max()) <= 0:
        return 0, max(0, len(v) - 1)
    v = v / float(v.max())
    k = max(3, len(v) // max(8, int(smooth_div)))
    smooth = np.convolve(v, np.ones(k) / k, mode="same")
    on = smooth >= min_frac
    if not on.any():
        return 0, len(v) - 1
    idx = np.where(on)[0]
    return int(idx[0]), int(idx[-1])


def _longest_run(on: np.ndarray) -> tuple[int, int, int]:
    best_a, best_b, best_n = 0, max(0, len(on) - 1), -1
    i = 0
    n = len(on)
    while i < n:
        if not on[i]:
            i += 1
            continue
        j = i
        while j < n and on[j]:
            j += 1
        if j - i > best_n:
            best_a, best_b, best_n = i, j - 1, j - i
        i = j
    return best_a, best_b, max(0, best_n)


def _otsu(values: np.ndarray) -> int:
    hist, _ = np.histogram(values.astype(np.uint8), bins=256, range=(0, 256))
    total = int(hist.sum())
    if total <= 0:
        return 0
    sum_total = float(np.dot(np.arange(256), hist))
    sum_b = 0.0
    w_b = 0
    var_max = -1.0
    thresh = 0
    for t in range(256):
        w_b += int(hist[t])
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * int(hist[t])
        m_b = sum_b / w_b
        m_f = (sum_total - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > var_max:
            var_max = var
            thresh = t
    return thresh


def _largest_tall(fg: np.ndarray, min_area: int, min_width: int) -> np.ndarray | None:
    """밝은 세로 샘플만 고른다. 케이블·가로 배경 박스는 버린다."""
    try:
        from scipy import ndimage

        labels, n = ndimage.label(fg)
    except Exception:
        return None
    if n == 0:
        return None
    h, w = fg.shape
    cy, cx = h / 2.0, w / 2.0
    best = None
    best_score = -1.0
    for k in range(1, n + 1):
        mask = labels == k
        area = int(mask.sum())
        if area < min_area:
            continue
        ys, xs = np.where(mask)
        bw = int(xs.max() - xs.min() + 1)
        bh = int(ys.max() - ys.min() + 1)
        if bw < min_width:
            continue
        ar = bh / max(1, bw)
        if ar < 1.15:
            continue
        dist = ((ys.mean() - cy) / h) ** 2 + ((xs.mean() - cx) / w) ** 2
        score = area * ar / (1.0 + 10.0 * dist)
        if score > best_score:
            best_score = score
            best = mask
    return best


def _peel_backdrop(mask: np.ndarray, val: np.ndarray) -> np.ndarray:
    """샘플 뒤에 붙은 회색 박스를 떼고 세로로 긴 블록만 남긴다."""
    if not mask.any():
        return mask
    h, w = mask.shape
    ys, xs = np.where(mask)
    bw = int(xs.max() - xs.min() + 1)
    bh = int(ys.max() - ys.min() + 1)
    if bh >= 1.2 * bw and bw <= 0.38 * w:
        return mask
    thr = max(150, _otsu(val[mask]))
    bright = mask & (val >= thr)
    closed = Image.fromarray(bright.astype(np.uint8) * 255)
    closed = closed.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(3))
    bright = np.asarray(closed) > 127
    peeled = _largest_tall(bright, min_area=int(0.06 * w * h), min_width=120)
    return peeled if peeled is not None else mask


def _clip_dense_band(mask: np.ndarray) -> np.ndarray:
    """가로 판 위아래에 붙은 폼·전선을 밀도 높은 띠만 남겨 떼 낸다."""
    if not mask.any():
        return mask
    ys, xs = np.where(mask)
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    if bw < 1.15 * bh:
        return mask
    row = mask[y0 : y1 + 1, x0 : x1 + 1].mean(axis=1)
    on = row >= max(0.25, 0.45 * float(row.max()))
    r0, r1, run = _longest_run(on)
    if run < 180 or run >= 0.82 * bh:
        return mask
    pad = max(4, int(run * 0.04))
    a = y0 + max(0, r0 - pad)
    b = y0 + min(bh - 1, r1 + pad)
    out = np.zeros_like(mask)
    out[a : b + 1, x0 : x1 + 1] = mask[a : b + 1, x0 : x1 + 1]
    return out


def _largest_mask(fg: np.ndarray) -> np.ndarray:
    try:
        from scipy import ndimage

        labels, n = ndimage.label(fg)
    except Exception:
        return fg
    if n <= 1:
        return fg
    h, w = fg.shape
    cy, cx = h / 2.0, w / 2.0
    best = fg
    best_score = -1.0
    for k in range(1, n + 1):
        mask = labels == k
        area = int(mask.sum())
        if area < 0.02 * w * h or area > 0.55 * w * h:
            continue
        ys, xs = np.where(mask)
        dist = ((ys.mean() - cy) / h) ** 2 + ((xs.mean() - cx) / w) ** 2
        score = area / (1.0 + 10.0 * dist)
        if score > best_score:
            best_score = score
            best = mask
    return best


def _as_rgb(image: Image.Image | np.ndarray | str | Path) -> Image.Image:
    if isinstance(image, (str, Path)):
        return Image.open(image).convert("RGB")
    if isinstance(image, Image.Image):
        return image.convert("RGB")
    return Image.fromarray(np.asarray(image))


def _hue_delta(hue: np.ndarray, target: int) -> np.ndarray:
    delta = np.abs(hue.astype(int) - int(target))
    return np.minimum(delta, 256 - delta)


def color_mask(
    image: Image.Image | np.ndarray | str | Path,
    color: dict[str, Any],
    search_box: tuple[int, int, int, int] | None = None,
) -> np.ndarray:
    rgb_img = _as_rgb(image)
    w, h = rgb_img.size
    hsv = np.asarray(rgb_img.convert("HSV"))
    hue = hsv[:, :, 0].astype(int)
    sat = hsv[:, :, 1].astype(int)
    val = hsv[:, :, 2].astype(int)
    h0 = int(color["h"])
    s0 = int(color["s"])
    v0 = int(color["v"])
    h_tol = int(color.get("h_tol") or 22)
    s_tol = int(color.get("s_tol") or 50)
    v_tol = int(color.get("v_tol") or 70)
    sat_ok = np.abs(sat - s0) <= s_tol
    val_ok = np.abs(val - v0) <= v_tol
    if s0 < 40:
        fg = sat_ok & val_ok & (sat <= max(s0 + s_tol, 60))
    else:
        fg = (_hue_delta(hue, h0) <= h_tol) & sat_ok & val_ok
    if search_box is not None:
        x0, y0, x1, y1 = (int(v) for v in search_box)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w - 1, x1), min(h - 1, y1)
        window = np.zeros((h, w), dtype=bool)
        window[y0 : y1 + 1, x0 : x1 + 1] = True
        fg &= window
    return fg


def _largest_blob(fg: np.ndarray, min_frac: float = 0.008) -> np.ndarray:
    try:
        from scipy import ndimage

        labels, n = ndimage.label(fg)
    except Exception:
        return fg
    if n <= 1:
        return fg
    h, w = fg.shape
    cy, cx = h / 2.0, w / 2.0
    best = fg
    best_score = -1.0
    min_area = int(min_frac * w * h)
    for k in range(1, n + 1):
        mask = labels == k
        area = int(mask.sum())
        if area < min_area:
            continue
        ys, xs = np.where(mask)
        dist = ((ys.mean() - cy) / h) ** 2 + ((xs.mean() - cx) / w) ** 2
        score = area / (1.0 + 10.0 * dist)
        if score > best_score:
            best_score = score
            best = mask
    return best


def _convex_mask(mask: np.ndarray) -> np.ndarray:
    ys, xs = np.where(mask)
    if xs.size < 8:
        return mask
    pts = np.column_stack((xs.astype(np.float64), ys.astype(np.float64)))
    try:
        from scipy.spatial import ConvexHull

        hull = ConvexHull(pts)
        verts = pts[hull.vertices]
    except Exception:
        return mask
    h, w = mask.shape
    canvas = Image.new("L", (w, h), 0)
    poly = [(int(round(x)), int(round(y))) for x, y in verts]
    ImageDraw.Draw(canvas).polygon(poly, fill=255)
    return np.asarray(canvas) > 127


def paint_outside(rgb: Image.Image, mask: np.ndarray) -> Image.Image:
    """마스크 밖은 검게 둔다. 면만 남기고 받침대·전선·조명은 뺀다."""
    arr = np.asarray(rgb.convert("RGB"))
    if mask.shape[0] != arr.shape[0] or mask.shape[1] != arr.shape[1]:
        scaled = Image.fromarray(mask.astype(np.uint8) * 255).resize(
            (arr.shape[1], arr.shape[0]), Image.NEAREST
        )
        mask = np.asarray(scaled) > 127
    out = np.zeros_like(arr)
    out[mask] = arr[mask]
    return Image.fromarray(out)


def _solid_silhouette(mask: np.ndarray) -> np.ndarray:
    """색이 다른 스크래치·찌그러짐을 안으로 남긴 외곽만 남긴다."""
    if not mask.any():
        return mask
    filled = mask
    try:
        from scipy import ndimage

        filled = ndimage.binary_closing(mask, iterations=6)
        filled = ndimage.binary_fill_holes(filled)
    except Exception:
        closed = Image.fromarray(mask.astype(np.uint8) * 255)
        closed = closed.filter(ImageFilter.MaxFilter(15)).filter(ImageFilter.MinFilter(9))
        filled = np.asarray(closed) > 127
    blob = _largest_blob(filled)
    return _convex_mask(blob)


def pick_color(
    image: Image.Image | np.ndarray | str | Path,
    x: int,
    y: int,
    radius: int = 8,
) -> dict[str, Any] | None:
    """클릭 주변 중앙값 HSV. 하이라이트·검정 배경은 빼다."""
    rgb_img = _as_rgb(image)
    w, h = rgb_img.size
    x = max(0, min(w - 1, int(x)))
    y = max(0, min(h - 1, int(y)))
    x0, x1 = max(0, x - radius), min(w, x + radius + 1)
    y0, y1 = max(0, y - radius), min(h, y + radius + 1)
    patch = np.asarray(rgb_img.convert("HSV"))[y0:y1, x0:x1]
    if patch.size == 0:
        return None
    val = patch[:, :, 2]
    keep = val >= 40
    if int(keep.sum()) < 6:
        keep = np.ones(val.shape, dtype=bool)
    pixels = patch[keep]
    h0 = int(np.median(pixels[:, 0]))
    s0 = int(np.median(pixels[:, 1]))
    v0 = int(np.median(pixels[:, 2]))
    return {
        "picked": True,
        "h": h0,
        "s": s0,
        "v": v0,
        "h_tol": 22 if s0 >= 40 else 40,
        "s_tol": 50,
        "v_tol": 70,
    }


def tols_from_allowance(allowance: int, color: dict[str, Any] | None) -> dict[str, int]:
    span = max(10, min(90, int(allowance)))
    sat = int((color or {}).get("s") or 40)
    return {
        "h_tol": 40 if sat < 40 else max(10, int(round(span * 0.45))),
        "s_tol": span,
        "v_tol": min(255, span + 18),
    }


def detect_mask_ex(
    image: Image.Image | np.ndarray | str | Path,
    color: dict[str, Any] | None = None,
    search_box: tuple[int, int, int, int] | None = None,
    use_seg: bool = True,
) -> tuple[Image.Image, np.ndarray, str]:
    """마스크와 출처(세그/색/회색)."""
    rgb_img = _as_rgb(image)
    seg_note = ""
    raw = None
    if use_seg:
        try:
            from sample_ov import available as ov_ok
            from sample_ov import predict_mask as ov_mask

            if ov_ok():
                raw = ov_mask(rgb_img)
                if raw is None or not raw.any():
                    seg_note = "Geti 미검출"
        except Exception as exc:
            raw = None
            seg_note = "Geti 오류"
            print(f"[sample-roi] Geti 실패: {exc}", flush=True)
        if raw is not None and raw.any():
            return rgb_img, _solid_silhouette(raw), "Geti"
        try:
            from sample_seg import predict_mask

            raw = predict_mask(rgb_img)
            if raw is None or not raw.any():
                seg_note = (seg_note + " · " if seg_note else "") + "세그 미검출"
        except Exception as exc:
            raw = None
            err = str(exc)
            extra = "세그 모듈없음" if "ultralytics" in err or "No module" in err else "세그 오류"
            seg_note = (seg_note + " · " if seg_note else "") + extra
            print(f"[sample-roi] 세그 실패: {exc}", flush=True)
        if raw is not None and raw.any():
            return rgb_img, _solid_silhouette(raw), "세그"
    if color:
        raw = color_mask(rgb_img, color, search_box=search_box)
        source = "색" if not seg_note else f"색 · {seg_note}"
        return rgb_img, _solid_silhouette(raw), source
    w, h = rgb_img.size
    rgb = np.asarray(rgb_img)
    hsv = np.asarray(rgb_img.convert("HSV"))
    sat = hsv[:, :, 1].astype(int)
    val = hsv[:, :, 2].astype(int)
    chroma = rgb.max(axis=2).astype(int) - rgb.min(axis=2).astype(int)
    x0s, x1s = int(w * 0.10), int(w * 0.90)
    fg = np.zeros((h, w), dtype=bool)
    fg[:, x0s:x1s] = (val[:, x0s:x1s] >= 125) & (chroma[:, x0s:x1s] <= 75) & (sat[:, x0s:x1s] <= 95)
    closed = Image.fromarray(fg.astype(np.uint8) * 255)
    closed = closed.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.MinFilter(5))
    fg = np.asarray(closed) > 127
    mask = _largest_mask(fg)
    mask = _peel_backdrop(mask, val)
    source = "회색" if not seg_note else f"회색 · {seg_note}"
    return rgb_img, _clip_dense_band(mask), source


def detect_mask(
    image: Image.Image | np.ndarray | str | Path,
    color: dict[str, Any] | None = None,
    search_box: tuple[int, int, int, int] | None = None,
    use_seg: bool = True,
) -> tuple[Image.Image, np.ndarray]:
    rgb_img, mask, _source = detect_mask_ex(
        image, color=color, search_box=search_box, use_seg=use_seg
    )
    return rgb_img, mask


def detect_box(
    image: Image.Image | np.ndarray | str | Path,
    color: dict[str, Any] | None = None,
    search_box: tuple[int, int, int, int] | None = None,
) -> tuple[int, int, int, int]:
    """샘플 bbox (x0, y0, x1, y1) inclusive. 실패해도 중앙 윈도우를 돌려준다."""
    rgb_img, mask = detect_mask(image, color=color, search_box=search_box)
    w, h = rgb_img.size
    if not mask.any():
        return _fallback_box(w, h)
    ys, xs = np.where(mask)
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    sub = mask[y0 : y1 + 1, x0 : x1 + 1]
    landscape = (x1 - x0 + 1) > 1.15 * (y1 - y0 + 1)
    if landscape:
        c0, c1 = _proj_span(sub.mean(axis=0), 0.08, smooth_div=80)
        r0, r1 = _proj_span(sub.mean(axis=1), 0.08, smooth_div=80)
    else:
        c0, c1 = _proj_span(sub.mean(axis=0), 0.22)
        r0, r1 = _proj_span(sub.mean(axis=1), 0.20)
    x0, x1 = x0 + c0, x0 + c1
    y0, y1 = y0 + r0, y0 + r1
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    if bw < 80 or bh < 80:
        return _fallback_box(w, h)
    px, py = max(8, int(bw * 0.06)), max(8, int(bh * 0.06))
    x0 = max(0, x0 - px)
    y0 = max(0, y0 - py)
    x1 = min(w - 1, x1 + px)
    y1 = min(h - 1, y1 + py)
    return x0, y0, x1, y1


def _fallback_box(w: int, h: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = _FALLBACK
    return int(w * x0), int(h * y0), int(w * x1) - 1, int(h * y1) - 1


def _fold_tilt(angle_deg: float) -> float:
    """가장 가까운 가로·세로에 맞추는 회전량. [-45, 45]."""
    a = (float(angle_deg) + 180.0) % 180.0
    if a > 90.0:
        a -= 180.0
    if a > 45.0:
        a -= 90.0
    elif a < -45.0:
        a += 90.0
    return a


def _oriented_rect(mask: np.ndarray) -> tuple[tuple[float, float], float, float, float] | None:
    """마스크를 가장 작게 감싸는 회전 상자. 기울어진 사각 면을 세운다."""
    try:
        from scipy import ndimage

        edge = mask ^ ndimage.binary_erosion(mask)
    except Exception:
        edge = mask
    ys, xs = np.where(edge)
    if xs.size < 40:
        ys, xs = np.where(mask)
    if xs.size < 80:
        return None
    pts = np.column_stack((xs.astype(np.float64), ys.astype(np.float64)))
    mean = pts.mean(axis=0)

    def score(tilt: float):
        c, s = np.cos(np.radians(tilt)), np.sin(np.radians(tilt))
        local = (pts - mean) @ np.array([[c, s], [-s, c]], dtype=np.float64).T
        mins = local.min(axis=0)
        maxs = local.max(axis=0)
        width = float(maxs[0] - mins[0] + 1)
        height = float(maxs[1] - mins[1] + 1)
        if width < 80 or height < 80:
            return None
        local_c = (mins + maxs) / 2.0
        inv = np.array([[c, -s], [s, c]], dtype=np.float64)
        center = mean + inv @ local_c
        return width * height, (float(center[0]), float(center[1])), width, height, float(tilt)

    best = None
    for deg in np.linspace(-45.0, 45.0, 31):
        hit = score(_fold_tilt(deg))
        if hit is not None and (best is None or hit[0] < best[0]):
            best = hit
    if best is None:
        return None
    mid = best[4]
    for deg in np.linspace(mid - 3.0, mid + 3.0, 25):
        hit = score(_fold_tilt(deg))
        if hit is not None and hit[0] < best[0]:
            best = hit
    return best[1], best[2], best[3], best[4]


def _mask_box(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    """마스크를 감싼 상자. 여백은 넣지 않는다."""
    if not mask.any():
        return None
    ys, xs = np.where(mask)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _aabb_crop(rgb: Image.Image, box: tuple[int, int, int, int]) -> "CropResult":
    x0, y0, x1, y1 = box
    cropped = rgb.crop((x0, y0, x1 + 1, y1 + 1))
    w, h = cropped.size
    return CropResult(
        image=cropped,
        box=box,
        affine=None,
        size=(w, h),
        corners=[[float(x0), float(y0)], [float(x1), float(y0)], [float(x1), float(y1)], [float(x0), float(y1)]],
    )


def _warp_obb(
    rgb: Image.Image,
    center: tuple[float, float],
    width: float,
    height: float,
    angle_deg: float,
    pad: float = 0.0,
) -> "CropResult":
    pw = width * pad
    ph = height * pad
    out_w = max(80, int(round(width + 2 * pw)))
    out_h = max(80, int(round(height + 2 * ph)))
    c, s = np.cos(np.radians(angle_deg)), np.sin(np.radians(angle_deg))
    ox, oy = (out_w - 1) / 2.0, (out_h - 1) / 2.0
    cx, cy = center
    a, b = float(c), float(-s)
    d, e = float(s), float(c)
    f_c = float(cx - a * ox - b * oy)
    f_f = float(cy - d * ox - e * oy)
    affine = (a, b, f_c, d, e, f_f)
    cropped = rgb.transform((out_w, out_h), Image.AFFINE, affine, resample=Image.BICUBIC)
    corners = []
    for x, y in ((0, 0), (out_w - 1, 0), (out_w - 1, out_h - 1), (0, out_h - 1)):
        corners.append([a * x + b * y + f_c, d * x + e * y + f_f])
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    w, h = rgb.size
    aabb = (
        int(max(0, np.floor(min(xs)))),
        int(max(0, np.floor(min(ys)))),
        int(min(w - 1, np.ceil(max(xs)))),
        int(min(h - 1, np.ceil(max(ys)))),
    )
    return CropResult(image=cropped, box=aabb, affine=affine, size=(out_w, out_h), corners=corners)


@dataclass
class CropResult:
    image: Image.Image
    box: tuple[int, int, int, int]
    affine: tuple[float, float, float, float, float, float] | None = None
    size: tuple[int, int] = (0, 0)
    corners: list[list[float]] = field(default_factory=list)
    source: str = ""
    mask: np.ndarray | None = None


def _with_meta(result: "CropResult", source: str, mask: np.ndarray | None) -> "CropResult":
    result.source = source
    result.mask = mask
    return result


def crop_detected(
    image: Image.Image | str | Path,
    color: dict[str, Any] | None = None,
    search_box: tuple[int, int, int, int] | None = None,
) -> CropResult:
    """물체를 찾아 면 축에 맞춰 세운 뒤, 마스크 테두리에 바짝 자른다."""
    rgb, mask, source = detect_mask_ex(image, color=color, search_box=search_box)
    if color and not mask.any():
        rgb, mask, source = detect_mask_ex(rgb)
    w, h = rgb.size
    if not mask.any():
        return _with_meta(_aabb_crop(rgb, _fallback_box(w, h)), f"{source} 실패", mask)
    rgb = paint_outside(rgb, mask)
    obb = _oriented_rect(mask)
    if obb is not None:
        return _with_meta(_warp_obb(rgb, *obb), source, mask)
    box = _mask_box(mask) or _fallback_box(w, h)
    return _with_meta(_aabb_crop(rgb, box), source, mask)


def crop_sample(
    image: Image.Image | str | Path,
    box: tuple[int, int, int, int] | None = None,
    color: dict[str, Any] | None = None,
    search_box: tuple[int, int, int, int] | None = None,
) -> tuple[Image.Image, tuple[int, int, int, int]]:
    if box is not None:
        rgb = _as_rgb(image)
        result = _aabb_crop(rgb, box)
        return result.image, result.box
    result = crop_detected(image, color=color, search_box=search_box)
    return result.image, result.box


CROP_COMMENT = b"DRCROP:sample-seg"
SAMPLE_SOURCES = ("Geti", "세그", "저장본")


class SampleNotFound(RuntimeError):
    """샘플 면을 못 찾아서 크롭하지 않는다."""


def is_sample_source(source: str) -> bool:
    return any(str(source).startswith(name) for name in SAMPLE_SOURCES)


def is_sample_cropped(path: str | Path) -> bool:
    try:
        comment = Image.open(path).info.get("comment") or b""
    except Exception:
        return False
    if isinstance(comment, str):
        comment = comment.encode("utf-8", "replace")
    return comment.startswith(b"DRCROP:")


def save_sample_crop(path: str | Path, image: Image.Image) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(path, quality=95, subsampling=0, comment=CROP_COMMENT)


def crop_and_replace(path: str | Path, cam_id: int, stage: int = 1, sample_only: bool = True) -> CropResult:
    """수집 장을 샘플 테두리만 남기고 덮어쓴다."""
    rgb, result = crop_camera(path, cam_id, stage=stage, sample_only=sample_only)
    save_sample_crop(path, result.image)
    return result


def crop_camera(
    path: str | Path,
    cam_id: int,
    stage: int = 1,
    sample_only: bool = True,
) -> tuple[Image.Image, CropResult]:
    """촬영본을 열고 Geti/세그로 샘플 면만 잘라 돌려준다."""
    from camera_calib import (
        fov_active,
        fov_box,
        image_has_fov_tag,
        load_fov,
        load_sample_color,
        open_camera_rgb,
    )

    path = Path(path)
    if is_sample_cropped(path):
        rgb = Image.open(path).convert("RGB")
        w, h = rgb.size
        return rgb, _with_meta(_aabb_crop(rgb, (0, 0, w - 1, h - 1)), "저장본", None)
    rgb = open_camera_rgb(path, cam_id, stage=stage)
    color = load_sample_color(cam_id, stage=stage)
    search = None
    if color and not image_has_fov_tag(path):
        fov = load_fov(cam_id, stage=stage)
        if fov_active(fov):
            search = fov_box(rgb.size[0], rgb.size[1], fov)
    result = crop_detected(rgb, color=None if sample_only else color, search_box=None if sample_only else search)
    if sample_only and not is_sample_source(result.source):
        raise SampleNotFound(f"{path.name} 샘플 면 미검출 ({result.source or '없음'})")
    return rgb, result


def preview_crop(path: str | Path, cam_id: int, stage: int = 1) -> dict[str, Any]:
    """수집·판정 UI 확인용. 원본·크롭·출처를 한 번에 준다."""
    path = Path(path)
    already = is_sample_cropped(path)
    rgb, result = crop_camera(path, cam_id, stage=stage)
    return {
        "path": path,
        "rgb": rgb,
        "result": result,
        "already": already,
        "source": result.source or ("저장본" if already else ""),
        "size": result.size,
        "tilted": result.affine is not None,
    }


def median_box(boxes: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int] | None:
    if not boxes:
        return None
    arr = np.array(boxes, dtype=int)
    return tuple(int(x) for x in np.median(arr, axis=0))


def load_roi_index() -> dict[str, Any]:
    for path in (MODELS_ROI, ROI_PATH):
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    return {}


def face_fallback_box(face_key: str, width: int, height: int) -> tuple[int, int, int, int]:
    data = load_roi_index()
    raw = (data.get("faces") or {}).get(face_key) or {}
    box = raw.get("median")
    if not box or len(box) != 4:
        return _fallback_box(width, height)
    return int(box[0]), int(box[1]), int(box[2]), int(box[3])


def write_roi_index(faces: dict[str, list[tuple[int, int, int, int]]], dest: Path = ROI_PATH) -> dict[str, Any]:
    payload: dict[str, Any] = {"faces": {}}
    for key, boxes in faces.items():
        med = median_box(boxes)
        payload["faces"][key] = {
            "n": len(boxes),
            "median": list(med) if med else None,
        }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    MODELS_ROI.parent.mkdir(parents=True, exist_ok=True)
    MODELS_ROI.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def map_bbox_to_full(
    bbox: list[int],
    crop_box: tuple[int, int, int, int],
    full_w: int,
    full_h: int,
    affine: tuple[float, float, float, float, float, float] | None = None,
) -> list[int]:
    if affine is not None and bbox and len(bbox) >= 4:
        a, b, c, d, e, f = affine
        x0, y0, x1, y1 = (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))
        pts = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        xs, ys = [], []
        for x, y in pts:
            xs.append(a * x + b * y + c)
            ys.append(d * x + e * y + f)
        return [
            max(0, min(full_w, int(np.floor(min(xs))))),
            max(0, min(full_h, int(np.floor(min(ys))))),
            max(0, min(full_w, int(np.ceil(max(xs))))),
            max(0, min(full_h, int(np.ceil(max(ys))))),
        ]
    x0, y0, x1, y1 = crop_box
    if not bbox or len(bbox) < 4:
        return [x0, y0, x1, y1]
    return [
        max(0, min(full_w, x0 + int(bbox[0]))),
        max(0, min(full_h, y0 + int(bbox[1]))),
        max(0, min(full_w, x0 + int(bbox[2]))),
        max(0, min(full_h, y0 + int(bbox[3]))),
    ]


def _invert_affine(affine: tuple[float, float, float, float, float, float]) -> tuple[float, float, float, float, float, float]:
    a, b, c, d, e, f = (float(x) for x in affine)
    det = a * e - b * d
    if abs(det) < 1e-9:
        return affine
    ia, ib = e / det, -b / det
    id_, ie = -d / det, a / det
    ic = -(ia * c + ib * f)
    iff = -(id_ * c + ie * f)
    return ia, ib, ic, id_, ie, iff


def map_bbox_to_crop(
    bbox: list[float] | list[int],
    crop_box: tuple[int, int, int, int],
    crop_w: int,
    crop_h: int,
    affine: tuple[float, float, float, float, float, float] | None = None,
) -> list[int] | None:
    """전체 프레임 bbox → 크롭 좌표. 크롭 밖에 있으면 None."""
    if not bbox or len(bbox) < 4:
        return None
    x0, y0, x1, y1 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    pts = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    xs, ys = [], []
    if affine is not None:
        ia, ib, ic, id_, ie, iff = _invert_affine(affine)
        for x, y in pts:
            xs.append(ia * x + ib * y + ic)
            ys.append(id_ * x + ie * y + iff)
    else:
        ox, oy = float(crop_box[0]), float(crop_box[1])
        for x, y in pts:
            xs.append(x - ox)
            ys.append(y - oy)
    cx0 = int(np.floor(min(xs)))
    cy0 = int(np.floor(min(ys)))
    cx1 = int(np.ceil(max(xs)))
    cy1 = int(np.ceil(max(ys)))
    cx0 = max(0, min(crop_w, cx0))
    cy0 = max(0, min(crop_h, cy0))
    cx1 = max(0, min(crop_w, cx1))
    cy1 = max(0, min(crop_h, cy1))
    if cx1 - cx0 < 4 or cy1 - cy0 < 4:
        return None
    return [cx0, cy0, cx1, cy1]


def main():
    parser = argparse.ArgumentParser(description="샘플 ROI 미리보기")
    parser.add_argument("--preview", default="", help="오버레이 JPEG 저장 폴더")
    parser.add_argument("--sample", default="ok_001")
    args = parser.parse_args()
    from dataset_label import sample_dir

    folder = sample_dir(args.sample)
    dest = Path(args.preview) if args.preview else None
    if dest:
        dest.mkdir(parents=True, exist_ok=True)
    for face in FACES:
        path = shot_path(folder, face["stage"], face["cam"])
        if not path.is_file():
            continue
        result = crop_detected(path)
        print(
            f"{path.name}  {result.size[0]}x{result.size[1]}  "
            f"box={result.box}  rot={'yes' if result.affine else 'no'}"
        )
        if dest:
            vis = Image.open(path).convert("RGB")
            draw = ImageDraw.Draw(vis)
            if result.corners and len(result.corners) == 4:
                poly = [(int(x), int(y)) for x, y in result.corners]
                draw.polygon(poly, outline=(255, 48, 48), width=4)
            else:
                draw.rectangle(result.box, outline=(255, 48, 48), width=4)
            vis.save(dest / path.name, quality=85)
            result.image.save(dest / f"crop_{path.name}", quality=85)


if __name__ == "__main__":
    main()
