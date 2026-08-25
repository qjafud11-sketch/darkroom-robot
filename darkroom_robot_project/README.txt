암실 로봇 (SO-ARM101)

모터(J) 역할
  J1 베이스      좌우 회전
  J2 어깨        암실 쪽 숙이기·들기
  J3 팔꿈치      팔 길이·높이
  J4 손목(상하)  손목 위·아래
  J5 손목(롤)    180° 뒤집기
  J6 그리퍼      2200=닫힘  2550=열림

환경 설정 (최초 1회)
  cd darkroom_robot_project
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  python -m ipykernel install --user --name=darkroom-robot --display-name="darkroom-robot (.venv)"

관절값·속도 수정
  skills.py — 스텝마다 "속도 1줄 + 위치 1줄" (test.ipynb와 같은 형식)
    spd(j1=500, j2=500, j3=1000, j4=500, j5=1500, j6=500),
    pos({1: 2003, 2: 1953, 3: 3122, 4: 910, 5: 3030, 6: 2200},
        "[퇴출] 샘플 잡은 채 대기 자세"),
    투입 8스텝 · 뒤집기 13스텝 · 회수 7스텝 (구간당 중간동작 1개)
    속도 숫자↑ 그 관절이 먼저 도착, 숫자↓ 늦게 도착해서 경로가 휨
    spd() 줄은 다음 spd()까지 유지 — 속도가 같으면 생략 가능
  test.ipynb — 스텝별 단독 실행·티칭용
  티칭: 토크 해제 → 손으로 자세 → get_all_positions() → 해당 pos() 줄에 반영

실행
  source .venv/bin/activate
  python main.py              전체 (투입→검사1→뒤집기→검사2→판정→회수)
  python main.py insert       투입만
  python main.py flip         뒤집기만
  python main.py bringout     회수만
  python main.py insert --wait 3
  python main.py flip --grip-wait 0.8
  python main.py run --speed 250    속도 배율 (100~250%)
  python test_ui.py           테스트 버튼 UI

테스트 UI
  버튼 3개: 1차 샘플 넣기 / 2차 샘플 뒤집기 / 3차 샘플 꺼내기
  추가 대기(초)   도착 후 더 쉬는 시간. 기본 0
  그리퍼 대기(초) J6 스텝 4개에 한 번에 적용. 기본 0.5
  속도 배율       게이지바 또는 직접 입력 (100~250%). 기본 100

속도 배율
  spd()에 적어둔 숫자는 그대로 두고 전체 속도만 올림
  동작 모양을 만드는 건 한 스텝 안의 관절 간 비율이므로 전 관절에 같은 값을 곱함
  상한은 250% — 이 안에서는 28개 스텝 전부가 요청한 배율 그대로 따라옴
  물리적 한계는 273% (최고 속도값 1500 × 2.73 = SPEED_LIMIT 4095)
  더 올리려면 SCALE_MAX가 아니라 spd()의 1500(2차 12번·3차 7번 J5)을 먼저 낮출 것
  혹시 상한을 넘기는 스텝이 생기면 그 스텝 전체 배율을 같이 낮춰 비율을 지킴
  이때 로그에 [배율] 줄이 남음
  통신 사용 체크 시 TCP JSON 한 줄 전송
    event: task.start / task.done / task.error
    예) 추후 조명·카메라·아두이노 연동용 훅

스텝 넘어가는 시점
  팔 스텝  고정 쿨타임 없이 실제 도착을 보고 판단 — 속도를 올리면 대기도 알아서 짧아짐
           목표와 ARRIVE_TICK(20틱, 약 1.8°) 이내면 "도착"
           중력·마찰로 목표 앞에서 서면 "멈춤" (안 움직인 지 0.12초)
           ARRIVE_MAX(8초) 넘으면 경고 남기고 다음 스텝 — 끼임·통신두절에 안 멈춤
  그리퍼   J6만 바뀌는 스텝은 위치를 재지 않고 GRIP_WAIT(0.5초) 고정 대기
           1차 6번 · 2차 5·11번 · 3차 5번 — 이전 자세와 비교해 자동 판별
  로그에 스텝별 소요 시간과 완료 사유가 찍힘 — 느린 스텝을 보고 속도를 올리면 됨

설정 (skills.py)
  PORT         /dev/ttyACM0
  SPEED        500  — spd()에서 생략한 관절에 쓰이는 기본 속도
  WAIT         도착 후 추가 대기(초). 기본 0 = 연속 동작
               main.py --wait 또는 test_ui 입력칸으로 변경
  ARRIVE_TICK  도착 판정 여유(틱). 크게 하면 빨리 넘어가고 경로가 더 뭉개짐
  ARRIVE_MAX   안전 상한(초)
  GRIP_WAIT    그리퍼 스텝 고정 대기(초). 잡다가 놓치면 늘릴 것
  SPEED_SCALE  속도 배율. 1.0 = 100%. set_speed_scale(%)로도 변경
  SCALE_MIN    100% — 기준보다 느리게는 안 감
  SCALE_MAX    250% — UI 게이지바 범위도 여기를 따라감
  SPEED_LIMIT  4095 — 서보 속도 레지스터 상한

파일
  skills.py      스텝 정의 (속도·관절값·동작 설명)
  main.py        명령줄 · 전체 파이프라인
  driver_sdk.py  STS3215 시리얼
  test.ipynb     연결 / 티칭 / 스텝별 실행
  inspection.py  1·2차 검사·판정 (미구현)
