# Darkroom Robot · 암실 비전 검사 및 자동 분류

SO-ARM101 로봇팔, USB 카메라, Arduino 기반 조명과 서보를 연동해 암실에서 샘플을 촬영하고 표면 결함을 판정합니다. 운영 UI에서 자동 검사, 결과 확인, 검사 기록 조회를 할 수 있습니다.

[저장소 프로젝트 목록](../README.md#프로젝트-목록)

## 검사 흐름

1. 로봇팔이 샘플을 집어 암실에 넣습니다.
2. 조명을 켜고 카메라 1~4로 1차 촬영합니다.
3. 샘플을 뒤집은 뒤 카메라 3·4로 2차 촬영합니다.
4. 샘플을 회수하는 동안 촬영본을 바탕으로 OK/NG를 판정합니다.
5. 운영 UI의 전체 검사에서는 판정에 따라 샘플을 분류하고 결과를 기록합니다.

## 실행 환경

- **소프트웨어** · Linux, Python 3, Tkinter가 있는 그래픽 세션, `ffmpeg`, `v4l2-ctl`
- **장비** · SO-ARM101 로봇팔과 전원, USB 카메라, 조명 및 서보 제어용 Arduino 보드
- **모델과 데이터** · 모델 기반 판정에 사용할 가중치와 학습용 데이터셋은 별도로 준비합니다. 기본 판정 백엔드는 `unsup`이며, 면별 PatchCore 모델을 `~/darkroom_models/unsup/`에서 읽습니다. [판정 설정](ui_store.py) · [YOLO 가중치 경로](yolo_infer.py) · [데이터셋 정보](README.dataset.txt)

저장소 루트에서 다음을 실행합니다.

```bash
cd darkroom_robot_project
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python hw_ports.py
```

장치 목록에서 시리얼 장치와 카메라 슬롯을 확인합니다. Arduino 스케치와 장비별 연결 방법은 [조명 펌웨어](arduino_controller/arduino_controller.ino), [서보 펌웨어](servo_controller/servo_controller.ino), [기존 운영 문서](README.txt)를 참고하세요.

## 운영 UI와 실행기를 같은 PC에서 실행

장비가 연결된 Linux PC의 그래픽 세션에서 실행합니다. 카메라를 사용하는 수집 UI가 열려 있다면 먼저 닫습니다.

```bash
./start_inspect.sh --start
./start_inspect.sh --status
```

UI에 **실행기 연결됨**이 표시되면 검사를 시작할 수 있습니다. 종료하려면 `./start_inspect.sh --stop`을 실행합니다. 기본 로그는 `/tmp/darkroom/`에 저장됩니다.

## CLI로 실행

장비가 연결된 PC에서 가상환경을 활성화한 뒤 실행합니다.

```bash
python main.py --help
python main.py run
```

`run`은 샘플 투입부터 두 차례 촬영, 회수와 판정까지 실행합니다. 개별 동작은 `insert`, `flip`, `bringout` 등의 명령으로 실행할 수 있습니다. [CLI 코드](main.py) · [검사 시퀀스](pipeline.py)

## 구성 파일

| 기능 | 파일 |
| --- | --- |
| 운영 UI와 로봇 실행기 | [`gui_server.py`](gui_server.py), [`robot_client.py`](robot_client.py) |
| 로봇 동작, 검사 순서 | [`skills.py`](skills.py), [`pipeline.py`](pipeline.py) |
| 조명 및 카메라 연동, 장치 탐색 | [`inspection.py`](inspection.py), [`camera.py`](camera.py), [`hw_ports.py`](hw_ports.py) |
| 카메라 보정, 촬영 데이터 수집 | [`calib_ui.py`](calib_ui.py), [`collect_ui.py`](collect_ui.py) |
| 결함 판정 및 검사 기록 | [`judgment.py`](judgment.py), [`ui_store.py`](ui_store.py) |

촬영본은 `~/darkroom_captures/`에, 운영 UI의 검사 기록은 `~/darkroom_records/`에 저장됩니다.
