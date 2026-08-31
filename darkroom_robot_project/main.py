import argparse

from inspection import inspection_first, inspection_second
from pipeline import run_ready_sequence
from servo import home as servo_home
from servo import rotate_180 as servo_rotate_180
from skills import (
    set_grip_wait,
    set_speed_scale,
    set_wait,
    task_bringout,
    task_flip,
    task_insert,
)


def run():
    """준비된 줄: 투입 → 1차검사 → 뒤집기 → 2차검사 → 회수. 판정·분류는 아직 없음."""
    run_ready_sequence()


TASKS = {
    "run": run,
    "insert": task_insert,
    "inspect_1": inspection_first,
    "flip": task_flip,
    "servo": servo_rotate_180,
    "servo_home": servo_home,
    "inspect_2": inspection_second,
    "bringout": task_bringout,
}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="암실 로봇 — insert / inspect_1 / flip / servo / servo_home / inspect_2 / bringout / run",
    )
    parser.add_argument("task", nargs="?", default="run", choices=list(TASKS))
    parser.add_argument(
        "--wait",
        type=float,
        default=None,
        help="도착 후 추가 대기(초). 기본 0 = 연속 동작",
    )
    parser.add_argument(
        "--grip-wait",
        type=float,
        default=None,
        help="J6만 바뀌는 그리퍼 스텝의 고정 대기(초). 기본 0.5",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=None,
        help="속도 배율(%%). 100~250, 기본 100. 관절 비율은 유지됨",
    )
    args = parser.parse_args()
    if args.wait is not None:
        set_wait(args.wait)
    if args.grip_wait is not None:
        set_grip_wait(args.grip_wait)
    if args.speed is not None:
        set_speed_scale(args.speed)
    TASKS[args.task]()
