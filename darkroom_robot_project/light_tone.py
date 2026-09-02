"""조명 톤(색온도) 제어.

NeoPixel 흰색(r=g=b)은 낮은 밝기에서 파랗게 치우친다. 예전에는 그걸 카메라
화이트밸런스와 채도 0으로 눌렀는데, 그러면 카메라를 기본값에서 멀리 끌고 가야 했다.
조명 쪽 채널비로 잡으면 카메라는 기본값 근처에 두고도 중성 화면이 나온다.

원색으로 치우친 조명은 결함 대비가 아니라 색만 바꾼다. 그래서 백색 계열만 쓴다 —
따뜻한 흰색(2700K)에서 차가운 흰색(9000K)까지.

주의: 여기 K 값은 흑체 기준 근사다. NeoPixel 의 R/G/B 스펙트럼은 흑체가 아니라서
실제 색온도와는 다르다. 톤을 고르는 손잡이로만 쓰고, 판단은 카메라 실측으로 한다.
"""
from __future__ import annotations

from arduino_link import LIGHT_MAX

# 흑체 색온도의 sRGB 근사. 감마가 실린 값이라 그대로 PWM 에 넣으면 안 된다.
KELVIN_SRGB = {
    2700: (255, 169, 87),
    3000: (255, 180, 107),
    3500: (255, 196, 137),
    4000: (255, 209, 163),
    4500: (255, 219, 186),
    5000: (255, 228, 206),
    5500: (255, 236, 224),
    6000: (255, 243, 239),
    6500: (255, 249, 253),
    7000: (245, 243, 255),
    8000: (227, 233, 255),
    9000: (214, 225, 255),
}

SRGB_GAMMA = 2.2

# 펌웨어 MAX_TOTAL 과 같아야 한다. 흰색 B:80 의 채널 합(80*3)이 전류 예산이다.
MAX_TOTAL = 240

# 캘리브를 맞춘 톤. 실측으로 이 근처에서 카메라가 R≈G≈B 로 받는다.
# 파란 톤은 파란 채널만 먼저 포화돼서 노출을 못 올린다.
LIGHT_TONE_K = 3500


def tone_rgb(kelvin, level=LIGHT_MAX):
    """색온도와 밝기를 C 명령용 (r, g, b) 로 바꾼다.

    PWM 은 빛의 양에 선형이고 표의 sRGB 값은 감마가 실려 있다.
    그래서 선형으로 되돌린 비율로 채널을 나눈다. 안 그러면 따뜻한 톤이
    의도보다 훨씬 붉게 나온다.

    가장 센 채널이 항상 level 이 되게 맞춘다. 톤을 바꿔도 밝기가 같이
    흔들리면 스윕 결과를 비교할 수 없다.
    """
    if kelvin not in KELVIN_SRGB:
        raise ValueError(f"준비된 색온도가 아닙니다: {kelvin}")
    linear = [(v / 255.0) ** SRGB_GAMMA for v in KELVIN_SRGB[kelvin]]
    peak = max(linear) or 1.0
    level = max(0, min(int(level), LIGHT_MAX))
    return tuple(int(round(level * v / peak)) for v in linear)


def tone_command(kelvin, level=LIGHT_MAX):
    r, g, b = tone_rgb(kelvin, level)
    return f"C:{r},{g},{b}"


def budget_rgb(kelvin):
    """전류 예산을 다 쓰는 채널값. 톤을 맞추면 G·B 가 낮아 합이 남는다.

    그 여유를 살려야 카메라 3·4 처럼 먼 면도 노출이 찬다.
    """
    linear = [(v / 255.0) ** SRGB_GAMMA for v in KELVIN_SRGB[kelvin]]
    peak = max(linear) or 1.0
    ratio = [v / peak for v in linear]
    level = MAX_TOTAL / sum(ratio)
    return tuple(int(round(level * v)) for v in ratio)


def light_command(kelvin=LIGHT_TONE_K):
    """검사·수집이 쓰는 조명 명령. 톤을 맞추고 전류 예산을 다 쓴다."""
    r, g, b = budget_rgb(kelvin)
    return f"C:{r},{g},{b}"


def tones():
    return sorted(KELVIN_SRGB)
