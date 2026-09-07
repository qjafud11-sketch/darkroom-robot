"""SO-ARM101 동작 스텝.

J6  2100=닫힘  2550=열림. 집기는 task_grip / task_pick.
파이프라인은 pick → insert → flip → bringout → sort.
"""
import time

from driver_sdk import JOINT_IDS, STS3215Driver
from hw_ports import resolve_arm_port


PORT = resolve_arm_port()
SPEED = 500
WAIT = 0.0
ARRIVE_TICK = 28
STILL_TICK = 3
STILL_HOLD = 0.12
START_GRACE = 0.25
ARRIVE_MAX = 8.0
POLL = 0.01
STOP_DETECTION_ENABLED = True
GRIP_WAIT = 0.5
SPEED_SCALE = 1.0
SCALE_MIN = 1.0
SCALE_MAX = 2.5
SPEED_LIMIT = 4095

_driver = STS3215Driver(port=PORT)
_step_log = None
_eeprom_synced = False


def set_step_logger(fn):
    """테스트 UI가 스텝 로그를 받을 때 연결한다. None 이면 print만."""
    global _step_log
    _step_log = fn


def _log(message):
    print(message)
    if _step_log is not None:
        _step_log(message)


def prepare_arm():
    """메인 UI·테스트 UI 공통. 포트 연결 후 EEPROM이 JSON보다 좁으면 넓힌다."""
    global PORT, _eeprom_synced
    PORT = resolve_arm_port()
    _driver.port = PORT
    if not _driver.is_connected() and not _driver.connect():
        raise ConnectionError(f"SO-ARM101 연결 실패: {PORT}")
    if not _eeprom_synced:
        notes = _driver.expand_angle_limits_to_json()
        if notes:
            for note in notes:
                _log(f"[서보제한] {note}")
        else:
            _log("[서보제한] EEPROM이 분류 자세(J1=1050)를 허용함")
        _eeprom_synced = True
    _driver.set_all_torque(True)
    return PORT


def set_wait(seconds):
    global WAIT
    WAIT = float(seconds)


def set_grip_wait(seconds):
    global GRIP_WAIT
    GRIP_WAIT = float(seconds)


def set_speed_scale(percent):
    global SPEED_SCALE
    SPEED_SCALE = max(SCALE_MIN, min(SCALE_MAX, float(percent) / 100.0))
    return SPEED_SCALE * 100


def _scaled(speeds):
    raw = {sid: SPEED if speeds.get(sid) is None else speeds[sid] for sid in JOINT_IDS}
    top = max(raw.values())
    scale = min(SPEED_SCALE, SPEED_LIMIT / top) if top > 0 else SPEED_SCALE
    return {sid: max(1, round(value * scale)) for sid, value in raw.items()}, scale


def spd(j1=None, j2=None, j3=None, j4=None, j5=None, j6=None):
    return ("SPD", {1: j1, 2: j2, 3: j3, 4: j4, 5: j5, 6: j6})


def pos(pose, name=None):
    """위치 줄. name 은 로그용이며 없어도 된다."""
    if name:
        return ("POS", pose, name)
    return ("POS", pose)


def _grip_only(pose, previous):
    return (
        previous is not None
        and pose[6] != previous[6]
        and all(pose[sid] == previous[sid] for sid in (1, 2, 3, 4, 5))
    )


def _wait_arrival(goal):
    start = time.monotonic()
    reference = _driver.get_all_positions()
    reference_time = start

    while True:
        time.sleep(POLL)
        current = _driver.get_all_positions()
        stamp = time.monotonic()
        elapsed = stamp - start
        gaps = {
            sid: abs(goal[sid] - current[sid])
            for sid in JOINT_IDS
            if current[sid] is not None
        }

        if gaps and max(gaps.values()) <= ARRIVE_TICK:
            return elapsed, "도착", current

        moved = max(
            (
                abs(current[sid] - reference[sid])
                for sid in JOINT_IDS
                if current[sid] is not None and reference[sid] is not None
            ),
            default=0,
        )
        if moved > STILL_TICK:
            reference, reference_time = current, stamp
        elif (
            STOP_DETECTION_ENABLED
            and elapsed >= START_GRACE
            and stamp - reference_time >= STILL_HOLD
        ):
            return elapsed, "중간정지", current

        if elapsed >= ARRIVE_MAX:
            return elapsed, "시간초과", current


def _joint_gap_lines(requested, sent, current):
    lines = []
    for sid in JOINT_IDS:
        want = requested.get(sid)
        goal = sent.get(sid)
        now = current.get(sid)
        if now is None:
            lines.append(f"J{sid} 현재값 읽기 실패  입력 {want}")
            continue
        delta = now - want if want is not None else None
        text = f"J{sid} 입력 {want}  현재 {now}  차이 {delta:+d}틱"
        if want is not None and goal is not None and want != goal:
            text += f"  (제한으로 {want}→{goal} 전송)"
        lines.append(text)
    return lines


def _log_arrival(index, label, requested, sent, current, took, reason):
    if reason == "도착":
        _log(f"[이동] {index} {label}  {took:.2f}초  목표 ±{ARRIVE_TICK}틱 안 도착")
        return
    if reason == "중간정지":
        why = (
            f"목표 ±{ARRIVE_TICK}틱에 못 미친 채 {STILL_HOLD:.2f}초 이상 거의 안 움직임 "
            f"(부하·기구 간섭·토크 한계 가능)"
        )
    elif reason == "시간초과":
        why = f"{ARRIVE_MAX:.0f}초 안에 목표에 못 도달"
    elif reason == "그리퍼 고정대기":
        why = f"그리퍼만 바꿔 {GRIP_WAIT:.2f}초 고정대기"
    else:
        why = reason
    _log(f"[미도달] {index} {label}  {took:.2f}초  이유: {why}")
    for line in _joint_gap_lines(requested, sent, current):
        _log(f"[미도달] {line}")


def run_steps(title, steps, step_offset=0, step_waits=None):
    prepare_arm()

    _log(f"=== {title} === (속도 배율 {SPEED_SCALE * 100:.0f}%)")
    last = step_offset + sum(kind == "POS" for kind, *_ in steps)
    speeds = {}
    index = step_offset
    previous = None

    for kind, *rest in steps:
        if kind == "SPD":
            speeds = rest[0]
            continue

        pose = rest[0]
        name = rest[1] if len(rest) > 1 else None
        index += 1
        label = name or f"{index}번"
        _log(f"[{label} 자세 실행시작]")
        _log(f"[스텝 {index} 실행중]")

        scaled, scale = _scaled(speeds)
        speed_text = " | ".join(f"J{sid}:{scaled[sid]}" for sid in JOINT_IDS)
        _log(f"[속도] {index}번 스텝  {speed_text}")
        for sid in JOINT_IDS:
            _driver.set_speed(sid, scaled[sid])

        if scale < SPEED_SCALE - 0.01:
            _log(f"[배율] {index}번 스텝 속도 배율 {scale * 100:.0f}% 적용")

        applied_pose = _driver.set_all_positions(pose)
        changed = [
            f"J{sid}:{pose[sid]}→{applied_pose[sid]}"
            for sid in JOINT_IDS
            if pose.get(sid) != applied_pose.get(sid)
        ]
        if changed:
            _log(f"[캘리브 제한] {' | '.join(changed)}")

        if _grip_only(pose, previous):
            time.sleep(GRIP_WAIT)
        took, reason, current = _wait_arrival(applied_pose)
        previous = pose

        _log_arrival(index, label, pose, applied_pose, current, took, reason)
        _log(f"[{label} 자세 실행종료]")

        if step_waits and index in step_waits:
            delay = float(step_waits[index])
            _log(f"[대기] {delay}초")
            time.sleep(delay)
        elif index != last and WAIT > 0:
            _log(f"[대기] {WAIT}초")
            time.sleep(WAIT)


def list_pose_steps(steps):
    """POS만 모아 (번호, 속도, 자세, 이름) 목록."""
    speeds = {}
    index = 0
    items = []
    for kind, *rest in steps:
        if kind == "SPD":
            speeds = rest[0]
            continue
        index += 1
        pose = rest[0]
        name = rest[1] if len(rest) > 1 else None
        items.append((index, dict(speeds), pose, name))
    return items


def run_pose_step(title, steps, step_no):
    """한 POS 스텝만 실행. step_no 는 1부터."""
    items = list_pose_steps(steps)
    if step_no < 1 or step_no > len(items):
        raise ValueError(f"{title} 스텝은 1~{len(items)}: {step_no}")
    index, speeds, pose, name = items[step_no - 1]
    subset = [("SPD", speeds), ("POS", pose, name) if name else ("POS", pose)]
    run_steps(f"{title} {index}/{len(items)}", subset, step_offset=index - 1)
    return pose


# 샘플 집기
GRIP = [
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 1
    pos({1: 2735, 2: 745, 3: 3165, 4: 1185, 5: 1020, 6: 2550}),
    spd(j1=500, j2=700, j3=700, j4=200, j5=500, j6=500),  # 2
    pos({1: 2735, 2: 2670, 3: 2098, 4: 1206, 5: 1020, 6: 2497}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 3
    pos({1: 2735, 2: 2670, 3: 2098, 4: 1206, 5: 1020, 6: 2100}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 4
    pos({1: 2735, 2: 1856, 3: 3111, 4: 1028, 5: 1020, 6: 2100}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 5
    pos({1: 2735, 2: 745, 3: 3165, 4: 1185, 5: 1020, 6: 2100}),
]


# 투입
INSERT = [
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 1
    pos({1: 2020, 2: 1953, 3: 3122, 4: 910, 5: 1020, 6: 2100}),
    spd(j1=500, j2=350, j3=850, j4=300, j5=500, j6=500),  # 2
    pos({1: 2020, 2: 2336, 3: 2191, 4: 1270, 5: 1020, 6: 2100}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 3
    pos({1: 2020, 2: 2245, 3: 2170, 4: 1525, 5: 1020, 6: 2100}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 4
    pos({1: 2020, 2: 2590, 3: 1817, 4: 1565, 5: 1020, 6: 2100}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 5
    pos({1: 2020, 2: 2590, 3: 1817, 4: 1565, 5: 1020, 6: 2550}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 6
    pos({1: 2020, 2: 2245, 3: 2170, 4: 1525, 5: 1020, 6: 2550}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 7
    pos({1: 2020, 2: 2036, 3: 2391, 4: 1570, 5: 1020, 6: 2550}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 8
    pos({1: 2020, 2: 1953, 3: 3122, 4: 910, 5: 1020, 6: 2550}),
]
INSERT_STEP_WAITS = {4: 0.5, 5: 0.5}


# 플립
FLIP = [
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 1
    pos({1: 2020, 2: 1953, 3: 3122, 4: 910, 5: 1020, 6: 2550}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 2
    pos({1: 2020, 2: 2036, 3: 2391, 4: 1570, 5: 1020, 6: 2550}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 3
    pos({1: 2020, 2: 2245, 3: 2170, 4: 1525, 5: 1020, 6: 2550}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 4
    pos({1: 2020, 2: 2590, 3: 1817, 4: 1565, 5: 1020, 6: 2550}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 5
    pos({1: 2020, 2: 2590, 3: 1817, 4: 1565, 5: 1020, 6: 2100}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 6
    pos({1: 2020, 2: 2145, 3: 2170, 4: 1525, 5: 1020, 6: 2100}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=1000, j6=500),  # 7
    pos({1: 2020, 2: 2145, 3: 2170, 4: 1525, 5: 3060, 6: 2100}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 8
    pos({1: 2060, 2: 2280, 3: 2170, 4: 1525, 5: 3060, 6: 2100}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 9
    pos({1: 2060, 2: 2580, 3: 1817, 4: 1525, 5: 3060, 6: 2100}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 10
    pos({1: 2060, 2: 2580, 3: 1817, 4: 1525, 5: 3060, 6: 2550}),
    spd(j1=500, j2=700, j3=500, j4=500, j5=500, j6=500),  # 11
    pos({1: 2060, 2: 2195, 3: 2170, 4: 1525, 5: 3060, 6: 2550}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=900, j6=500),  # 12
    pos({1: 2020, 2: 2036, 3: 2391, 4: 1570, 5: 3060, 6: 2550}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=900, j6=500),  # 13
    pos({1: 2020, 2: 1953, 3: 3122, 4: 910, 5: 1020, 6: 2550}),
]


# 브링아웃
BRINGOUT = [
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 1
    pos({1: 2020, 2: 1953, 3: 3122, 4: 910, 5: 1020, 6: 2550}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 2
    pos({1: 2020, 2: 2036, 3: 2391, 4: 1570, 5: 1020, 6: 2550}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 3
    pos({1: 2020, 2: 2245, 3: 2170, 4: 1525, 5: 1020, 6: 2550}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 4
    pos({1: 2020, 2: 2590, 3: 1817, 4: 1565, 5: 1020, 6: 2550}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 5
    pos({1: 2020, 2: 2590, 3: 1817, 4: 1565, 5: 1020, 6: 2100}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 6
    pos({1: 2020, 2: 2485, 3: 1817, 4: 1565, 5: 1020, 6: 2100}),
    spd(j1=500, j2=500, j3=700, j4=500, j5=500, j6=500),  # 7
    pos({1: 2020, 2: 2036, 3: 2391, 4: 1570, 5: 1020, 6: 2100}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 8
    pos({1: 2020, 2: 1953, 3: 3122, 4: 910, 5: 1020, 6: 2100}),
]
BRINGOUT_STEP_WAITS = {4: 0.5, 5: 0.5}


# 양품분류
SORT_OK = [
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 1
    pos({1: 2020, 2: 1366, 3: 3122, 4: 910, 5: 1020, 6: 2100}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 2
    pos({1: 1400, 2: 1366, 3: 3122, 4: 910, 5: 1020, 6: 2100}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 3
    pos({1: 1400, 2: 2380, 3: 2400, 4: 1200, 5: 1020, 6: 2100}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 4
    pos({1: 1400, 2: 2380, 3: 2400, 4: 1200, 5: 1020, 6: 2550}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 5
    pos({1: 1400, 2: 1366, 3: 3122, 4: 910, 5: 1020, 6: 2550}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 6
    pos({1: 2020, 2: 1366, 3: 3122, 4: 910, 5: 1020, 6: 2550}),
]


# 불량분류
SORT_NG = [
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 1
    pos({1: 2020, 2: 1366, 3: 3122, 4: 910, 5: 1020, 6: 2100}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 2
    pos({1: 1050, 2: 1366, 3: 3122, 4: 910, 5: 1020, 6: 2100}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 3
    pos({1: 1050, 2: 2290, 3: 2720, 4: 1020, 5: 1020, 6: 2100}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 4
    pos({1: 1050, 2: 2290, 3: 2720, 4: 1020, 5: 1020, 6: 2550}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 5
    pos({1: 1050, 2: 1366, 3: 3122, 4: 910, 5: 1020, 6: 2550}),
    spd(j1=500, j2=500, j3=500, j4=500, j5=500, j6=500),  # 6
    pos({1: 2020, 2: 1366, 3: 3122, 4: 910, 5: 1020, 6: 2550}),
]


def task_grip():
    run_steps("샘플 집기", GRIP)


def task_pick():
    """파이프라인 0번 집기. 최신 스킬셋 task_grip 과 같다."""
    task_grip()


def task_insert():
    run_steps("투입", INSERT, step_waits=INSERT_STEP_WAITS)


def task_flip():
    run_steps("플립", FLIP)


def task_bringout():
    run_steps("브링아웃", BRINGOUT, step_waits=BRINGOUT_STEP_WAITS)


def task_sort_ok():
    run_steps("양품분류", SORT_OK)


def task_sort_ng():
    run_steps("불량분류", SORT_NG)


def task_sort(result):
    """판정값에 맞는 분류만 실행한다."""
    verdict = str(result).strip().upper()
    if verdict == "OK":
        return task_sort_ok()
    if verdict == "NG":
        return task_sort_ng()
    raise ValueError(f"지원하지 않는 분류 결과: {result!r} (OK 또는 NG 필요)")
