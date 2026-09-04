"""양품 60세트로 면별 PatchCore 비지도 학습.

입력:  ~/darkroom_captures/unsup/<face>/train/good  (50) + test/good (10)
출력:  ~/darkroom_models/unsup/<face>/  ckpt + threshold.json

Intel Arc B580 (XPU) 우선, 없으면 CUDA, 없으면 CPU.
PatchCore 는 에폭 1. 불량 박스가 없어서 문턱은 양품 holdout 최댓값 × 1.10.
채점은 회색 판 안쪽만 본다 (전선·지그·크롭 테두리 제외).

  python unsup_train.py
  python unsup_train.py --force
  python unsup_train.py --face face1_cam1_s1
"""
from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from datetime import datetime
from pathlib import Path

from dataset_label import FACES, UNSUP_DIR

MODELS_DIR = Path.home() / "darkroom_models" / "unsup"
IMAGE_SIZE = (256, 256)
BACKBONE = "resnet18"
BATCH = 16
THRESHOLD_MARGIN = 1.10


def pick_device():
    import torch

    # This box is an Intel Arc B580 (torch.xpu). Prefer XPU over CUDA even if both exist.
    if hasattr(torch, "xpu") and torch.xpu.is_available():
        return torch.device("xpu")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def device_name(device) -> str:
    import torch

    try:
        if device.type == "xpu":
            return torch.xpu.get_device_name(0)
        if device.type == "cuda":
            return torch.cuda.get_device_name(0)
    except Exception:
        pass
    return str(device)


def _datamodule(face_key: str):
    from anomalib.data import Folder
    from anomalib.data.utils.split import TestSplitMode, ValSplitMode

    return Folder(
        name=face_key,
        root=UNSUP_DIR / face_key,
        normal_dir="train/good",
        normal_test_dir="test/good",
        abnormal_dir=None,
        train_batch_size=BATCH,
        eval_batch_size=BATCH,
        num_workers=0,
        test_split_mode=TestSplitMode.FROM_DIR,
        val_split_mode=ValSplitMode.NONE,
        seed=0,
    )


def _model():
    from anomalib.models import Patchcore

    pre = Patchcore.configure_pre_processor(image_size=IMAGE_SIZE)
    return Patchcore(
        backbone=BACKBONE,
        layers=("layer2", "layer3"),
        pre_trained=True,
        coreset_sampling_ratio=0.1,
        pre_processor=pre,
        evaluator=False,
        visualizer=False,
    )


def _prepare_batch(batch, model, device):
    transform = getattr(model.pre_processor, "transform", None)
    if transform is not None:
        batch.image, batch.gt_mask = transform(batch.image, batch.gt_mask)
    image = batch.image.to(device, non_blocking=True)
    if hasattr(batch, "replace"):
        return batch.replace(image=image)
    batch.image = image
    return batch


def _scores_from_predictions(predictions) -> list[float]:
    scores = []
    if predictions is None:
        return scores
    batches = predictions if isinstance(predictions, list) else [predictions]
    for batch in batches:
        raw = getattr(batch, "pred_score", None)
        if raw is None and isinstance(batch, dict):
            raw = batch.get("pred_score")
        if raw is None:
            continue
        if hasattr(raw, "detach"):
            raw = raw.detach().cpu().flatten().tolist()
        elif hasattr(raw, "tolist"):
            raw = raw.tolist()
        else:
            raw = [float(raw)]
        scores.extend(float(x) for x in raw)
    return scores


def _threshold(scores: list[float]) -> dict:
    import numpy as np

    arr = np.array(scores, dtype=float)
    if arr.size == 0:
        return {"threshold": 0.0, "n": 0}
    mx = float(arr.max())
    p99 = float(np.quantile(arr, 0.99))
    return {
        "threshold": mx * THRESHOLD_MARGIN,
        "mean": float(arr.mean()),
        "max": mx,
        "p95": float(np.quantile(arr, 0.95)),
        "p99": p99,
        "margin": THRESHOLD_MARGIN,
        "n": int(arr.size),
    }


def _save_ckpt(model, out: Path) -> str:
    import lightning.pytorch as pl

    ckpt = out / "Patchcore" / "gpu" / "weights" / "lightning" / "model.ckpt"
    ckpt.parent.mkdir(parents=True, exist_ok=True)
    cpu_model = model.to("cpu")
    trainer = pl.Trainer(
        accelerator="cpu",
        devices=1,
        logger=False,
        enable_checkpointing=False,
        max_epochs=1,
        num_sanity_val_steps=0,
    )
    trainer.strategy.connect(cpu_model)
    trainer.save_checkpoint(str(ckpt))
    return str(ckpt)


def train_face(face_key: str, device) -> dict:
    import torch

    root = UNSUP_DIR / face_key
    train_n = len(list((root / "train" / "good").glob("*.jpg")))
    test_n = len(list((root / "test" / "good").glob("*.jpg")))
    if train_n < 5:
        raise RuntimeError(f"{face_key} 학습 양품이 너무 적다: {train_n}")

    out = MODELS_DIR / face_key
    out.mkdir(parents=True, exist_ok=True)
    gpu = device_name(device)
    t0 = time.perf_counter()
    print(f"\n===== {face_key}  {device} ({gpu})  train {train_n}  test {test_n}  → {out} =====", flush=True)

    datamodule = _datamodule(face_key)
    datamodule.setup()
    model = _model()
    model.to(device)
    model.train()

    n_seen = 0
    for i, batch in enumerate(datamodule.train_dataloader()):
        batch = _prepare_batch(batch, model, device)
        if i == 0:
            print(
                f"[{face_key}] batch image {tuple(batch.image.shape)} on {batch.image.device}",
                flush=True,
            )
        model.training_step(batch, 0)
        n_seen += int(batch.image.shape[0])
    print(f"[{face_key}] 특징 추출 {n_seen}장 완료, coreset…", flush=True)
    model.fit()
    mb = getattr(model.model, "memory_bank", None)
    if mb is not None and hasattr(mb, "shape"):
        print(f"[{face_key}] coreset {tuple(mb.shape)}", flush=True)
    model._is_fitted = torch.tensor([True], device=device)

    scores: list[float] = []
    model.eval()
    with torch.no_grad():
        from unsup_infer import masked_anomaly_score

        loader = datamodule.test_dataloader() if test_n else datamodule.train_dataloader()
        for batch in loader:
            batch = _prepare_batch(batch, model, device)
            pred = model.model(batch.image)
            amap = getattr(pred, "anomaly_map", None)
            if amap is None:
                raw = getattr(pred, "pred_score", None)
                if raw is None:
                    continue
                scores.extend(float(x) for x in raw.detach().cpu().flatten().tolist())
                continue
            for i in range(int(batch.image.shape[0])):
                scores.append(masked_anomaly_score(amap[i], batch.image[i]))

    stats = _threshold(scores)
    (out / "threshold.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    weights = _save_ckpt(model, out)
    elapsed = time.perf_counter() - t0
    summary = {
        "face": face_key,
        "backbone": BACKBONE,
        "crop": "fov+oriented",
        "train_n": train_n,
        "train_seen": n_seen,
        "test_n": test_n,
        "seconds": round(elapsed, 1),
        "ckpt": weights,
        "threshold": stats.get("threshold"),
        "score_stats": stats,
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "torch": torch.__version__,
        "device": str(device),
        "device_name": gpu,
    }
    (out / "model.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[{face_key}] ckpt={weights}  thr={stats.get('threshold')}  {elapsed:.1f}s",
        flush=True,
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description="면별 PatchCore 학습")
    parser.add_argument("--face", default="", help="하나만 학습. 비우면 6면 전부")
    parser.add_argument("--force", action="store_true", help="이미 있는 면도 다시 학습")
    args = parser.parse_args()
    os.environ.setdefault("OMP_NUM_THREADS", "8")

    device = pick_device()
    gpu = device_name(device)
    print(f"장치 {device} ({gpu})", flush=True)
    if device.type == "cpu":
        print("경고: GPU/XPU를 못 찾았다. CPU로 느리다.", flush=True)

    faces = [f["key"] for f in FACES]
    if args.face:
        if args.face not in faces:
            raise SystemExit(f"모르는 면: {args.face}  (가능: {', '.join(faces)})")
        faces = [args.face]

    results = []
    failed = []
    for key in faces:
        existing = MODELS_DIR / key / "model.json"
        if existing.is_file() and not args.face and not args.force:
            print(f"건너뜀 {key} — 이미 {existing}")
            results.append(json.loads(existing.read_text(encoding="utf-8")))
            continue
        try:
            results.append(train_face(key, device))
        except Exception as exc:
            traceback.print_exc()
            print(f"실패 {key}: {exc}", flush=True)
            failed.append(key)

    if failed:
        print(f"\n재시도 {len(failed)}면: {', '.join(failed)}", flush=True)
        still = []
        for key in failed:
            try:
                results.append(train_face(key, device))
            except Exception as exc:
                traceback.print_exc()
                print(f"재시도 실패 {key}: {exc}", flush=True)
                still.append(key)
        failed = still

    index = {
        "models": results,
        "root": str(MODELS_DIR),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "device": str(device),
        "device_name": gpu,
        "failed": failed,
    }
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    (MODELS_DIR / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n완료 {len(results)}면  → {MODELS_DIR}  ({device} {gpu})")
    if failed:
        raise SystemExit(f"실패한 면: {', '.join(failed)}")


if __name__ == "__main__":
    main()
