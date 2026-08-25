"""암실 검사 — 카메라·센서 연동 시 이 파일만 구현하면 됩니다."""


def inspection_first():
    """1차 검사 (투입 직후). 촬영·측정 코드를 넣을 자리."""
    print("\n[검사] 1차 — 투입 완료 후")


def inspection_second():
    """2차 검사 (뒤집기 직후). 뒤집힌 샘플 확인."""
    print("\n[검사] 2차 — 뒤집기 완료 후")


def judge_product():
    """OK/NG 판정. 아직 미구현 — 항상 OK 반환."""
    return "OK"
