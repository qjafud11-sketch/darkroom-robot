"""크롭된 YOLO 데이터로 스크래치·찌그러짐을 학습한다.

입력은 256이 아니라 640. 샘플만 잘린 뒤 글자가 작아지지 않게 둔다.

  python yolo_train.py
  python yolo_train.py --epochs 80 --imgsz 640
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

DATA = Path.home() / "darkroom_captures" / "yolo" / "data.yaml"
OUT = Path.home() / "darkroom_models" / "yolo"


def pick_device() -> str:
    try:
        import torch

        # Intel Arc B580: ultralytics 8.4 accepts device=xpu.
        if hasattr(torch, "xpu") and torch.xpu.is_available():
            return "xpu"
        if torch.cuda.is_available():
            return "0"
    except Exception:
        pass
    return "cpu"


def main():
    parser = argparse.ArgumentParser(description="크롭 YOLO 학습")
    parser.add_argument("--data", default=str(DATA))
    parser.add_argument("--model", default="yolo11n.pt")
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--epochs", type=int, default=80)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--device", default="")
    args = parser.parse_args()
    data = Path(args.data)
    if not data.is_file():
        raise SystemExit(f"먼저 yolo_prepare.py 를 실행하세요: {data}")

    from ultralytics import YOLO

    device = args.device or pick_device()
    os.environ.setdefault("OMP_NUM_THREADS", "8")
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"data {data}  model {args.model}  imgsz {args.imgsz}  device {device}", flush=True)
    model = YOLO(args.model)
    model.train(
        data=str(data),
        imgsz=args.imgsz,
        epochs=args.epochs,
        batch=args.batch,
        device=device,
        project=str(OUT),
        name="crop640",
        exist_ok=True,
        pretrained=True,
        patience=20,
        close_mosaic=10,
        hsv_h=0.015,
        hsv_s=0.4,
        hsv_v=0.3,
        degrees=10.0,
        fliplr=0.5,
        workers=2,
    )
    print(f"완료  {OUT / 'crop640'}", flush=True)


if __name__ == "__main__":
    main()
