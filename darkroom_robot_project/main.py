import argparse

from inspection import inspection_first, inspection_second, judge_product
from skills import (
    set_grip_wait,
    set_speed_scale,
    set_wait,
    task_bringout,
    task_flip,
    task_insert,
)


def run():
    """전체 파이프라인: 투입 → 1차검사 → 뒤집기 → 2차검사 → 판정 → 회수"""
    print("\n########## 암실 검수 시작 ##########")
    task_insert()
    inspection_first()
    task_flip()
    inspection_second()
    print(f"\n[판정] {judge_product()}")
    task_bringout()
    print("########## 완료 ##########\n")


TASKS = {
    "run": run,
    "insert": task_insert,
    "flip": task_flip,
    "bringout": task_bringout,
}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="암실 로봇 — insert(투입) / flip(뒤집기) / bringout(회수) / run(전체)",
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
