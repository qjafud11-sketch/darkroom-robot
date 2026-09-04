"""검사 기록 기반 통계·리포트 데이터 및 차트 이미지 (PIL)."""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ui_store import format_judged_at, list_records

# 다크 테마 (gui_common COLORS 와 맞춤)
_C = {
    "bg": "#182136",
    "panel": "#182136",
    "plot": "#0F1626",
    "grid": "#243049",
    "border": "#2A3752",
    "text": "#9CA9BC",
    "text_hi": "#F8FAFC",
    "accent": "#22D3EE",
    "accent_dim": "#164E63",
    "ok": "#4ADE80",
    "ng": "#FB7185",
    "warn": "#FBBF24",
    "ucl": "#FB7185",
    "lcl": "#4ADE80",
    "cl": "#22D3EE",
}

DEFECT_LABELS = {
    "unknown": "이상(비지도)",
    "scratch": "스크래치",
    "contamination": "이물",
    "dent": "찌그러짐",
    "edge_break": "모서리 깨짐",
    "dimension": "치수 불량",
    "code_fail": "코드 인식 실패",
    "mock_defect": "테스트 불량",
}

PIE_COLORS = ("#22D3EE", "#FB7185", "#FBBF24", "#4ADE80", "#A78BFA", "#F472B6", "#64748B")

_FONT_CACHE: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}
_FONT_RESOLVED: tuple[str, int, str, int] | None = None  # regular_path, regular_idx, bold_path, bold_idx


def _fc_match(pattern: str) -> tuple[str, int] | None:
    import subprocess

    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}|%{index}", pattern],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        raw = result.stdout.strip()
        if not raw or raw == "|":
            return None
        path, _, index_s = raw.partition("|")
        if not path or not Path(path).is_file():
            return None
        idx = int(index_s) if index_s.isdigit() else 0
        return path, idx
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _resolve_fonts() -> tuple[str, int, str, int]:
    """한글 지원 폰트 경로 (regular, bold)."""
    global _FONT_RESOLVED
    if _FONT_RESOLVED is not None:
        return _FONT_RESOLVED

    regular = _fc_match("Noto Sans CJK KR:style=Regular") or _fc_match("Noto Sans CJK KR")
    bold = _fc_match("Noto Sans CJK KR:style=Bold")

    if not regular:
        for path, idx in (
            ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 1),
            ("/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc", 1),
            ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf", 0),
            ("/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf", 0),
        ):
            if Path(path).is_file():
                regular = (path, idx)
                break

    if not bold:
        for path, idx in (
            ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 1),
            ("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf", 0),
        ):
            if Path(path).is_file():
                bold = (path, idx)
                break

    if not regular:
        win_fonts = (
            Path(r"C:\Windows\Fonts\malgun.ttf"),
            Path(r"C:\Windows\Fonts\malgunbd.ttf"),
            Path(r"C:\Windows\Fonts\segoeui.ttf"),
        )
        for path in win_fonts:
            if path.is_file():
                regular = (str(path), 0)
                break

    if regular and not bold:
        bold = regular

    if not regular:
        _FONT_RESOLVED = ("", 0, "", 0)
        return _FONT_RESOLVED

    reg_path, reg_idx = regular
    bold_path, bold_idx = bold or regular
    _FONT_RESOLVED = (reg_path, reg_idx, bold_path, bold_idx)
    return _FONT_RESOLVED


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached

    reg_path, reg_idx, bold_path, bold_idx = _resolve_fonts()
    path, idx = (bold_path, bold_idx) if bold else (reg_path, reg_idx)
    if path:
        try:
            font = ImageFont.truetype(path, size, index=idx)
            _FONT_CACHE[key] = font
            return font
        except OSError:
            pass

    return ImageFont.load_default()


def defect_label(class_name: str) -> str:
    key = (class_name or "").strip().lower()
    return DEFECT_LABELS.get(key, class_name or "기타")


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00").split("+")[0])
    except ValueError:
        return None


def _hex_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))


def _round_rect(draw: ImageDraw.ImageDraw, box: tuple, radius: int, **kwargs):
    try:
        draw.rounded_rectangle(box, radius=radius, **kwargs)
    except AttributeError:
        draw.rectangle(box, **kwargs)


def _chart_canvas(
    w: int, h: int, title: str, subtitle: str = "",
) -> tuple[Image.Image, ImageDraw.ImageDraw, int, tuple[int, int, int, int]]:
    img = Image.new("RGB", (w, h), _C["panel"])
    draw = ImageDraw.Draw(img)
    draw.text((8, 6), title, fill=_C["text_hi"], font=_font(15, bold=True))
    if subtitle:
        draw.text((8, 30), subtitle, fill=_C["text"], font=_font(9))
        plot_top = 52
    else:
        plot_top = 36
    plot_box = (8, plot_top, w - 8, h - 8)
    _round_rect(draw, plot_box, 20, fill=_C["plot"])
    return img, draw, plot_top + 10, plot_box


def yield_trend(records: list[dict[str, Any]], period: str = "day") -> tuple[list[str], list[float], list[int]]:
    """기간별 수율(%) — labels, rates, totals."""
    buckets: dict[str, list[str]] = defaultdict(list)
    for rec in records:
        dt = _parse_dt(rec.get("judged_at", ""))
        if not dt:
            continue
        if period == "week":
            key = dt.strftime("%Y-W%W")
            label = f"{dt.month}/{dt.day}주"
        elif period == "month":
            key = dt.strftime("%Y-%m")
            label = dt.strftime("%Y-%m")
        else:
            key = dt.strftime("%Y-%m-%d")
            label = dt.strftime("%m/%d")
        buckets[key].append(rec.get("verdict", ""))

    if not buckets and records:
        # 샘플이 적으면 건별로 표시
        ordered = sorted(records, key=lambda r: r.get("judged_at", ""))
        labels, rates, totals = [], [], []
        for rec in ordered:
            dt = _parse_dt(rec.get("judged_at", ""))
            labels.append(dt.strftime("%H:%M") if dt else "?")
            totals.append(1)
            rates.append(100.0 if rec.get("verdict") == "OK" else 0.0)
        return labels, rates, totals

    keys = sorted(buckets.keys())
    labels, rates, totals = [], [], []
    seen: dict[str, str] = {}
    for rec in records:
        dt = _parse_dt(rec.get("judged_at", ""))
        if not dt:
            continue
        if period == "week":
            k = dt.strftime("%Y-W%W")
            seen[k] = f"{dt.month}/{dt.day}주"
        elif period == "month":
            k = dt.strftime("%Y-%m")
            seen[k] = k
        else:
            k = dt.strftime("%Y-%m-%d")
            seen[k] = dt.strftime("%m/%d")

    for key in keys:
        verdicts = buckets[key]
        ok = sum(1 for v in verdicts if v == "OK")
        total = len(verdicts)
        labels.append(seen.get(key, key))
        rates.append(ok / total * 100.0 if total else 0.0)
        totals.append(total)
    return labels, rates, totals


def defect_type_breakdown(records: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for rec in records:
        if rec.get("verdict") != "NG":
            continue
        judgment = rec.get("judgment") or {}
        for item in judgment.get("defects") or []:
            name = defect_label(str(item.get("class_name", "기타")))
            counter[name] += 1
    return counter.most_common()


def spc_subgroups(records: list[dict[str, Any]], size: int = 2) -> list[dict[str, float]]:
    """검사 건별 품질 지표 → X-bar/R 부분군 (데모·소량 데이터용)."""
    ordered = sorted(records, key=lambda r: r.get("judged_at", ""))
    values: list[float] = []
    for rec in ordered:
        if rec.get("verdict") == "OK":
            values.append(98.0 + (rec.get("run_no", 0) % 3) * 0.4)
        else:
            judgment = rec.get("judgment") or {}
            defects = judgment.get("defects") or []
            if defects:
                scores = [float(d.get("score", 0.5)) * 100 for d in defects]
                values.append(max(55.0, 100.0 - max(scores)))
            else:
                values.append(72.0)

    groups: list[dict[str, float]] = []
    for i in range(0, len(values), size):
        chunk = values[i : i + size]
        if not chunk:
            continue
        xbar = sum(chunk) / len(chunk)
        rng = max(chunk) - min(chunk) if len(chunk) > 1 else 0.0
        groups.append({"xbar": xbar, "r": rng, "n": len(chunk)})
    return groups


def ng_history(records: list[dict[str, Any]], limit: int = 30) -> list[dict[str, Any]]:
    out = []
    for rec in records:
        if rec.get("verdict") != "NG":
            continue
        judgment = rec.get("judgment") or {}
        defects = judgment.get("defects") or []
        types = ", ".join(defect_label(str(d.get("class_name", ""))) for d in defects) or "—"
        out.append({
            "record": rec,
            "judged_at": rec.get("judged_at", ""),
            "judged_label": format_judged_at(rec.get("judged_at", "")),
            "run_no": rec.get("run_no"),
            "product": Path(rec.get("archive_path", "")).name or f"Run #{rec.get('run_no')}",
            "defect_types": types,
            "preview": rec.get("preview", ""),
            "message": rec.get("message", ""),
        })
        if len(out) >= limit:
            break
    return out


def render_yield_chart(labels: list[str], rates: list[float], period: str, size=(560, 300)) -> Image.Image:
    w, h = size
    img, draw, top, plot_box = _chart_canvas(w, h, "수율 트렌드", f"{period} · OK 비율")
    if not labels:
        draw.text((w // 2 - 40, h // 2), "데이터 없음", fill=_C["text"], font=_font(11))
        return img

    px0, py0, px1, py1 = plot_box
    margin_l, margin_r, margin_b = 42, 16, 28
    plot_w = (px1 - px0) - margin_l - margin_r
    plot_h = (py1 - py0) - margin_b - 8
    x0, y0 = px0 + margin_l, py0 + 8
    x1, y1 = x0 + plot_w, y0 + plot_h

    for pct in (0, 25, 50, 75, 100):
        y = y1 - plot_h * pct / 100
        draw.line([(x0, y), (x1, y)], fill=_C["grid"], width=1)
        draw.text((px0 + 6, y - 6), f"{pct:.0f}%", fill=_C["text"], font=_font(8))

    n = len(labels)
    points = []
    for i, rate in enumerate(rates):
        px = x0 + plot_w * (i + 0.5) / max(n, 1)
        py = y1 - plot_h * min(100, max(0, rate)) / 100
        points.append((px, py))

    if len(points) >= 2:
        area = [(x0, y1)] + points + [(points[-1][0], y1)]
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).polygon(area, fill=(*_hex_rgb(_C["accent"]), 46))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)
        draw.line(points, fill=_C["accent"], width=4, joint="curve")
    for px, py in points:
        draw.ellipse((px - 6, py - 6, px + 6, py + 6), fill=_C["plot"], outline=_C["accent"], width=3)

    for i, label in enumerate(labels):
        px = x0 + plot_w * (i + 0.5) / max(n, 1)
        tw = draw.textlength(label[:8], font=_font(8))
        draw.text((px - tw / 2, y1 + 4), label[:8], fill=_C["text"], font=_font(8))
    return img


def render_pie_chart(slices: list[tuple[str, int]], size=(560, 300)) -> Image.Image:
    w, h = size
    img, draw, top, plot_box = _chart_canvas(w, h, "불량 유형", "원인별 비중 · Donut")
    total = sum(c for _, c in slices)
    if total <= 0:
        draw.text((w // 2 - 55, h // 2), "NG 데이터 없음", fill=_C["text"], font=_font(11))
        return img

    px0, py0, px1, py1 = plot_box
    cx = px0 + (px1 - px0) * 0.38
    cy = py0 + (py1 - py0) * 0.52
    radius = min(px1 - px0, py1 - py0) * 0.36
    inner = radius * 0.64
    start = -90.0
    for i, (_label, count) in enumerate(slices):
        sweep = 360.0 * count / total
        color = PIE_COLORS[i % len(PIE_COLORS)]
        draw.pieslice(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            start=start, end=start + sweep, fill=color, outline=_C["plot"], width=3,
        )
        start += sweep
    draw.ellipse((cx - inner, cy - inner, cx + inner, cy + inner), fill=_C["plot"])
    draw.text((cx, cy - 8), f"{total}", fill=_C["text_hi"], font=_font(20, bold=True), anchor="mm")
    draw.text((cx, cy + 13), "건", fill=_C["text"], font=_font(9), anchor="mm")

    lx = px0 + (px1 - px0) * 0.62
    ly = py0 + 16
    for i, (label, count) in enumerate(slices[:6]):
        color = PIE_COLORS[i % len(PIE_COLORS)]
        pct = count / total * 100
        _round_rect(draw, (lx, ly, lx + 11, ly + 11), 5, fill=color)
        draw.text((lx + 16, ly - 1), f"{label}", fill=_C["text_hi"], font=_font(9))
        draw.text((lx + 16, ly + 11), f"{pct:.0f}%", fill=_C["text"], font=_font(8))
        ly += 34
    return img


def _spc_limits(values: list[float], k: float = 2.0) -> tuple[float, float, float]:
    if not values:
        return 90.0, 100.0, 80.0
    mean = sum(values) / len(values)
    if len(values) < 2:
        return mean, mean + 5, mean - 5
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    sigma = math.sqrt(var)
    return mean, mean + k * sigma, mean - k * sigma


def render_spc_xbar(groups: list[dict[str, float]], size=(560, 300)) -> Image.Image:
    w, h = size
    img, draw, top, plot_box = _chart_canvas(w, h, "SPC X-bar", "공정 평균 관리도")
    if not groups:
        draw.text((w // 2 - 40, h // 2), "데이터 없음", fill=_C["text"], font=_font(11))
        return img

    vals = [g["xbar"] for g in groups]
    cl, ucl, lcl = _spc_limits(vals)
    px0, py0, px1, py1 = plot_box
    margin_l, margin_r, margin_b = 42, 16, 24
    plot_w = (px1 - px0) - margin_l - margin_r
    plot_h = (py1 - py0) - margin_b - 8
    x0, y0 = px0 + margin_l, py0 + 8
    y1 = y0 + plot_h
    ymin, ymax = min(vals + [lcl]) - 2, max(vals + [ucl]) + 2
    span = max(ymax - ymin, 1)

    def y_map(v):
        return y1 - plot_h * (v - ymin) / span

    for label, val, color in (("LCL", lcl, _C["lcl"]), ("CL", cl, _C["cl"]), ("UCL", ucl, _C["ucl"])):
        yy = y_map(val)
        draw.line([(x0, yy), (x0 + plot_w, yy)], fill=color, width=2 if label == "CL" else 1)
        draw.text((px0 + 4, yy - 6), label, fill=color, font=_font(8))

    n = len(groups)
    pts = []
    for i, g in enumerate(groups):
        px = x0 + plot_w * (i + 0.5) / n
        py = y_map(g["xbar"])
        pts.append((px, py))
    if len(pts) > 1:
        draw.line(pts, fill=_C["accent"], width=3, joint="curve")
    for px, py in pts:
        draw.ellipse((px - 5, py - 5, px + 5, py + 5), fill=_C["plot"], outline=_C["accent"], width=3)
    return img


def render_spc_r(groups: list[dict[str, float]], size=(560, 300)) -> Image.Image:
    w, h = size
    img, draw, top, plot_box = _chart_canvas(w, h, "SPC R", "범위 관리도")
    if not groups:
        draw.text((w // 2 - 40, h // 2), "데이터 없음", fill=_C["text"], font=_font(11))
        return img

    vals = [g["r"] for g in groups]
    cl, ucl, _ = _spc_limits(vals, k=2.5)
    ucl = max(ucl, cl + 0.01)
    px0, py0, px1, py1 = plot_box
    margin_l, margin_r, margin_b = 42, 16, 24
    plot_w = (px1 - px0) - margin_l - margin_r
    plot_h = (py1 - py0) - margin_b - 8
    x0, y0 = px0 + margin_l, py0 + 8
    y1 = y0 + plot_h
    ymax = max(vals + [ucl]) * 1.2 or 1.0

    def y_map(v):
        return y1 - plot_h * v / ymax

    for label, val, color in (("UCL", ucl, _C["ucl"]), ("CL", cl, _C["cl"])):
        yy = y_map(val)
        draw.line([(x0, yy), (x0 + plot_w, yy)], fill=color, width=1)
        draw.text((px0 + 4, yy - 6), label, fill=color, font=_font(8))

    n = len(groups)
    bar_w = max(8, min(24, int(plot_w / max(n, 1) * 0.5)))
    for i, g in enumerate(groups):
        px = x0 + plot_w * (i + 0.5) / n
        py = y_map(g["r"])
        bar_top = min(py, y1 - 3)
        _round_rect(
            draw, (px - bar_w // 2, bar_top, px + bar_w // 2, y1),
            radius=min(bar_w // 2, int((y1 - bar_top) / 2)),
            fill=_C["warn"],
        )
    return img


def build_report_bundle(period: str = "day") -> dict[str, Any]:
    records = list_records()
    labels, rates, _ = yield_trend(records, period)
    slices = defect_type_breakdown(records)
    groups = spc_subgroups(records)
    period_ko = {"day": "일별", "week": "주별", "month": "월별"}.get(period, period)
    return {
        "records": records,
        "yield_chart": render_yield_chart(labels, rates, period_ko),
        "pie_chart": render_pie_chart(slices),
        "xbar_chart": render_spc_xbar(groups),
        "r_chart": render_spc_r(groups),
        "ng_log": ng_history(records),
        "summary": {
            "total": len(records),
            "ng": sum(1 for r in records if r.get("verdict") == "NG"),
            "defect_types": len(slices),
        },
    }
