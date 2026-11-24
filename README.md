# Darkroom Robot

SO-ARM101 로봇팔과 암실 카메라를 연동해 샘플의 표면 결함을 검사하고, OK/NG 판정에 따라 분류하는 프로젝트입니다.

## 프로젝트 목록

| 프로젝트 | 폴더 | 실행 및 운영 문서 |
| --- | --- | --- |
| **Darkroom Robot** · 암실 비전 검사 및 자동 분류 | [`darkroom_robot_project/`](darkroom_robot_project/) | [설치 및 실행](darkroom_robot_project/README.md) · [장비 설정과 티칭](darkroom_robot_project/README.txt) |

## 주요 진입점

- **운영 UI** · 검사 시작, 카메라 영상, 판정 및 검사 기록을 확인합니다. [`gui_server.py`](darkroom_robot_project/gui_server.py)
- **검사 실행기** · UI의 명령을 받아 로봇팔, 조명, 카메라, 판정 단계를 실행합니다. [`robot_client.py`](darkroom_robot_project/robot_client.py)
- **로컬 CLI** · 장비가 연결된 PC에서 검사 흐름이나 개별 단계를 실행합니다. [`main.py`](darkroom_robot_project/main.py)
