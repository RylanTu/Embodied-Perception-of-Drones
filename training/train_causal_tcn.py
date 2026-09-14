#!/usr/bin/env python3
"""Train and evaluate a strictly online, causal five-channel pressure detector."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import os
import random
from pathlib import Path
import sys
import types

# Some Windows Conda installations crash inside NumPy's optional BLAS-FPE
# startup probe because a system-wide MPI DLL shadows Conda's copy.  This
# opt-in bootstrap skips only that diagnostic; numerical operations are not
# changed.  Healthy environments use the normal import path.
if os.environ.get("PRESSURE_SKIP_NUMPY_BLAS_PROBE") == "1":
    _numpy_spec = importlib.util.find_spec("numpy")
    if _numpy_spec is None or _numpy_spec.origin is None:
        raise ImportError("NumPy is not installed")
    _numpy_module = types.ModuleType("numpy")
    _numpy_module.__file__ = _numpy_spec.origin
    _numpy_module.__package__ = "numpy"
    _numpy_module.__path__ = [str(Path(_numpy_spec.origin).parent)]
    _numpy_module.__spec__ = _numpy_spec
    sys.modules["numpy"] = _numpy_module
    _numpy_source = Path(_numpy_spec.origin).read_text(encoding="utf-8")
    _numpy_source = _numpy_source.replace(
        "    blas_fpe_check()\n    del blas_fpe_check", "    del blas_fpe_check"
    )
    exec(compile(_numpy_source, _numpy_spec.origin, "exec"), _numpy_module.__dict__)

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn


ALL_PRESSURE_COLUMNS = [f"pressure_pa_{index}" for index in range(1, 6)]


def load_runs(data_dir: Path, pressure_columns: list[str] | None = None) -> tuple[list[dict], list[str]]:
    pressure_columns = pressure_columns or ALL_PRESSURE_COLUMNS
    runs: list[dict] = []
    skipped: list[str] = []
    sensor_indices = [int(column.rsplit("_", 1)[1]) for column in pressure_columns]
    required_mask = sum(1 << (index - 1) for index in sensor_indices)
    for path in sorted(data_dir.rglob("*.csv")):
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
        except (OSError, UnicodeError, csv.Error) as exc:
            skipped.append(f"{path.name}: {exc}")
            continue
        if not rows or any(column not in rows[0] for column in pressure_columns):
            continue
        groups: dict[str, list[dict]] = {}
        for row in rows:
            if row.get("phase") not in (None, "", "measure"):
                continue
            trial_id = row.get("trial_id") or row.get("run_id") or path.stem
            groups.setdefault(trial_id, []).append(row)
        for trial_id, trial_rows in groups.items():
            try:
                if int(float(trial_rows[0].get("sensor_test_mode", "0") or 0)):
                    skipped.append(f"{path.name}/{trial_id}: simulated sensor data")
                    continue
                pressure = np.asarray(
                    [[float(row[column]) for column in pressure_columns] for row in trial_rows],
                    dtype=np.float32,
                ).T
                position = np.asarray(
                    [float(row.get("position_mm", "nan") or "nan") for row in trial_rows],
                    dtype=np.float32,
                )
                moving = np.asarray(
                    [int(float(row.get("moving", "1") or 0)) for row in trial_rows], dtype=np.int8
                )
                valid_mask = np.asarray(
                    [int(float(row.get("valid_mask", "31") or 0)) for row in trial_rows], dtype=np.int32
                )
                decel_mm = float(trial_rows[0].get("decel_mm", "75") or 75)
                label = int(float(trial_rows[0].get("label", "0") or 0))
            except (TypeError, ValueError):
                skipped.append(f"{path.name}/{trial_id}: invalid numeric value")
                continue
            if pressure.shape[1] < 220 or not np.all(np.isfinite(position)):
                skipped.append(f"{path.name}/{trial_id}: insufficient or invalid samples")
                continue
            if np.mean((valid_mask & required_mask) == required_mask) < 0.9:
                skipped.append(f"{path.name}/{trial_id}: selected-channel validity below 90%")
                continue
            for channel, sensor_index in enumerate(sensor_indices):
                valid = ((valid_mask & (1 << (sensor_index - 1))) != 0) & np.isfinite(pressure[channel])
                if not np.any(valid):
                    break
                indices = np.arange(pressure.shape[1])
                pressure[channel] = np.interp(indices, indices[valid], pressure[channel, valid])
            else:
                max_position = float(np.nanmax(position))
                decel_start = max_position - max(1.0, decel_mm)
                eligible = np.flatnonzero((position <= decel_start) & (moving == 1))
                if not len(eligible):
                    eligible = np.flatnonzero(position <= decel_start)
                if not len(eligible) or int(eligible[-1]) + 1 < 220:
                    skipped.append(f"{path.name}/{trial_id}: no usable pre-deceleration region")
                    continue
                runs.append({
                    "path": path,
                    "batch": path.name,
                    "name": trial_id,
                    "condition": trial_rows[0].get("condition", ""),
                    "label": label,
                    "pressure": pressure,
                    "position": position,
                    "cutoff": int(eligible[-1]) + 1,
                    "decel_start_mm": decel_start,
                })
    return runs, skipped


def split_from_manifest(runs: list[dict], manifest_path: Path) -> tuple[list[dict], list[dict], list[dict]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    names = [manifest[key] for key in ("training_batches", "validation_batches", "test_batches")]
    name_sets = [set(part) for part in names]
    result = [[run for run in runs if run["batch"] in allowed] for allowed in name_sets]
    for split_name, part in zip(("training", "validation", "test"), result):
        if {run["label"] for run in part} != {0, 1}:
            raise ValueError(f"{split_name} split does not contain both classes")
    return result[0], result[1], result[2]


def raw_windows(run: dict, window: int, stride: int) -> tuple[torch.Tensor, np.ndarray, np.ndarray]:
    cutoff = min(run["cutoff"], run["pressure"].shape[1])
    ends = np.arange(window, cutoff + 1, stride, dtype=np.int32)
    if not len(ends) or ends[-1] != cutoff:
        ends = np.append(ends, cutoff)
    pressure = run["pressure"]
    samples = np.stack([pressure[:, end - window:end] for end in ends])[:, :, None, :]
    return torch.from_numpy(samples), ends, run["position"][ends - 1]


class CausalBlock(nn.Module):
    def __init__(self, channels: int, kernel_size: int, dilation: int):
        super().__init__()
        self.left = (kernel_size - 1) * dilation
        self.conv1 = nn.Conv2d(channels, channels, (1, kernel_size), dilation=(1, dilation))
        self.conv2 = nn.Conv2d(channels, channels, (1, kernel_size), dilation=(1, dilation))

    def forward(self, value: torch.Tensor) -> torch.Tensor:
        residual = value
        value = F.relu(self.conv1(F.pad(value, (self.left, 0, 0, 0))))
        value = self.conv2(F.pad(value, (self.left, 0, 0, 0)))
        return F.relu(value + residual)


class CausalPressureTCN(nn.Module):
    def __init__(self, feature_mean: torch.Tensor, feature_std: torch.Tensor, width: int = 24):
        super().__init__()
        feature_count = int(feature_mean.numel())
        self.register_buffer("feature_mean", feature_mean.reshape(1, feature_count, 1, 1))
        self.register_buffer("feature_std", feature_std.reshape(1, feature_count, 1, 1))
        self.input = nn.Conv2d(feature_count, width, (1, 1))
        self.blocks = nn.ModuleList([CausalBlock(width, 3, dilation) for dilation in (1, 2, 4, 8, 16)])
        self.output = nn.Conv2d(width, 1, (1, 1))
        self.endpoint = nn.Conv2d(1, 1, (1, 200), bias=False)
        with torch.no_grad():
            self.endpoint.weight.zero_()
            self.endpoint.weight[..., -1] = 1.0
        self.endpoint.weight.requires_grad_(False)

    @staticmethod
    def derive(raw: torch.Tensor) -> torch.Tensor:
        centered = raw - raw[..., :1]
        delta = torch.cat((torch.zeros_like(raw[..., :1]), raw[..., 1:] - raw[..., :-1]), dim=-1)
        return torch.cat((centered, delta), dim=1)

    def forward(self, raw: torch.Tensor) -> torch.Tensor:
        value = (self.derive(raw) - self.feature_mean) / self.feature_std
        value = F.relu(self.input(value))
        for block in self.blocks:
            value = block(value)
        # A frozen convolution selects the final causal activation.  Unlike an
        # ONNX Slice node, this keeps the exported output shape fully static for
        # ATC while preserving exact endpoint semantics.
        value = self.endpoint(self.output(value))
        if torch.onnx.is_in_onnx_export():
            # Deployment is intentionally fixed to batch=1 for Atlas ATC.
            return value.reshape(1, 1, 1, 1)
        return value


def augment(raw: torch.Tensor) -> torch.Tensor:
    batch, channels = raw.shape[:2]
    gain = 1.0 + 0.03 * torch.randn(batch, channels, 1, 1, device=raw.device)
    offset = 0.02 * torch.randn(batch, channels, 1, 1, device=raw.device)
    slope = 0.025 * torch.randn(batch, channels, 1, 1, device=raw.device)
    timeline = torch.linspace(-1.0, 1.0, raw.shape[-1], device=raw.device).reshape(1, 1, 1, -1)
    noise = 0.008 * torch.randn_like(raw)
    return raw * gain + offset + slope * timeline + noise


def prepare_runs(runs: list[dict], window: int, stride: int) -> list[dict]:
    prepared = []
    for run in runs:
        windows, ends, positions = raw_windows(run, window, stride)
        prepared.append({**run, "windows": windows, "ends": ends, "window_positions": positions})
    return prepared


def normalization(prepared: list[dict]) -> tuple[torch.Tensor, torch.Tensor]:
    samples = torch.cat([run["windows"] for run in prepared])
    features = CausalPressureTCN.derive(samples)
    mean = features.mean(dim=(0, 2, 3))
    std = features.std(dim=(0, 2, 3)).clamp_min(1e-4)
    return mean, std


def mil_loss(model: nn.Module, runs: list[dict], device: torch.device, training: bool,
             top_k: int, negative_aux: float) -> torch.Tensor:
    windows = torch.cat([run["windows"] for run in runs]).to(device)
    if training:
        windows = augment(windows)
    logits = model(windows).flatten()
    aggregates, labels, negative_logits = [], [], []
    cursor = 0
    for run in runs:
        count = len(run["windows"])
        segment = logits[cursor:cursor + count]
        cursor += count
        aggregates.append(torch.topk(segment, min(top_k, count)).values.mean())
        labels.append(float(run["label"]))
        if run["label"] == 0:
            negative_logits.append(segment)
    target = torch.tensor(labels, dtype=torch.float32, device=device)
    positive = max(1.0, float(target.sum()))
    negative = max(1.0, float(len(target) - target.sum()))
    loss = F.binary_cross_entropy_with_logits(
        torch.stack(aggregates), target, pos_weight=torch.tensor(negative / positive, device=device)
    )
    if negative_logits:
        loss = loss + negative_aux * F.softplus(torch.cat(negative_logits)).mean()
    return loss


@torch.no_grad()
def replay(model: nn.Module, runs: list[dict], device: torch.device, batch_size: int) -> list[dict]:
    model.eval()
    result = []
    for run in runs:
        windows = run["windows"]
        chunks = []
        for start in range(0, len(windows), batch_size):
            logits = model(windows[start:start + batch_size].to(device)).flatten()
            chunks.append(torch.sigmoid(logits).cpu())
        result.append({"run": run, "probability": torch.cat(chunks).numpy()})
    return result


def first_hit(probability: np.ndarray, threshold: float, consecutive: int) -> int | None:
    count = 0
    for index, value in enumerate(probability):
        count = count + 1 if value >= threshold else 0
        if count >= consecutive:
            return index - consecutive + 1
    return None


def run_metrics(replays: list[dict], threshold: float, consecutive: int) -> dict:
    tp = fp = tn = fn = 0
    alarm_positions, leads = [], []
    for item in replays:
        hit = first_hit(item["probability"], threshold, consecutive)
        truth = item["run"]["label"] == 1
        alarm = hit is not None
        tp += int(alarm and truth); fp += int(alarm and not truth)
        tn += int(not alarm and not truth); fn += int(not alarm and truth)
        if alarm:
            position = float(item["run"]["window_positions"][hit])
            alarm_positions.append(position)
            if truth:
                leads.append(float(item["run"]["decel_start_mm"] - position))
    precision = tp / max(1, tp + fp)
    recall = tp / max(1, tp + fn)
    specificity = tn / max(1, tn + fp)
    return {
        "threshold": threshold, "consecutive_hits": consecutive,
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "accuracy": (tp + tn) / max(1, tp + fp + tn + fn),
        "precision": precision, "recall": recall, "specificity": specificity,
        "balanced_accuracy": (recall + specificity) / 2,
        "f1": 2 * precision * recall / max(1e-12, precision + recall),
        "detected_positive_lead_mm_median": float(np.median(leads)) if leads else None,
        "alarm_position_mm_median": float(np.median(alarm_positions)) if alarm_positions else None,
        "runs": len(replays),
    }


def choose_threshold(replays: list[dict], consecutive: int) -> tuple[float, dict]:
    candidates = np.arange(0.05, 0.996, 0.005)
    scored = [(run_metrics(replays, float(value), consecutive), float(value)) for value in candidates]
    # A single maximum at the edge of a probability plateau is unstable on a
    # small validation set.  Take the middle threshold among settings within
    # one percentage point of the best balanced accuracy and with best recall.
    best_balanced = max(pair[0]["balanced_accuracy"] for pair in scored)
    plateau = [pair for pair in scored if pair[0]["balanced_accuracy"] >= best_balanced - 0.01]
    best_recall = max(pair[0]["recall"] for pair in plateau)
    plateau = [pair for pair in plateau if pair[0]["recall"] == best_recall]
    _, threshold = plateau[len(plateau) // 2]
    threshold = round(threshold, 3)
    return threshold, run_metrics(replays, threshold, consecutive)


def prediction_rows(replays: list[dict], threshold: float, consecutive: int) -> list[dict]:
    rows = []
    for item in replays:
        hit = first_hit(item["probability"], threshold, consecutive)
        run = item["run"]
        rows.append({
            "batch": run["batch"], "trial_id": run["name"], "condition": run["condition"],
            "label": run["label"], "alarm": int(hit is not None),
            "max_probability": float(np.max(item["probability"])),
            "alarm_position_mm": "" if hit is None else float(run["window_positions"][hit]),
            "decel_start_mm": float(run["decel_start_mm"]),
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("--output", type=Path, default=Path("training/output_causal_tcn"))
    parser.add_argument("--split-manifest", type=Path, required=True)
    parser.add_argument("--channels", default="1,2,3,4,5",
                        help="comma-separated pressure sensor numbers, for example 2,3,4")
    parser.add_argument("--window-samples", type=int, default=200)
    parser.add_argument("--train-stride", type=int, default=20)
    parser.add_argument("--replay-stride", type=int, default=1)
    parser.add_argument("--epochs", type=int, default=120)
    parser.add_argument("--run-batch-size", type=int, default=16)
    parser.add_argument("--inference-batch-size", type=int, default=512)
    parser.add_argument("--learning-rate", type=float, default=8e-4)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--negative-aux", type=float, default=0.15)
    parser.add_argument("--consecutive-hits", type=int, default=3)
    parser.add_argument("--patience", type=int, default=22)
    parser.add_argument("--seed", type=int, default=20260912)
    args = parser.parse_args()
    if args.window_samples != 200:
        raise ValueError("this ATC-static architecture requires --window-samples 200")
    channels = [int(value.strip()) for value in args.channels.split(",") if value.strip()]
    if not channels or len(channels) != len(set(channels)) or any(value not in range(1, 6) for value in channels):
        raise ValueError("--channels must contain unique sensor numbers from 1 to 5")
    pressure_columns = [f"pressure_pa_{value}" for value in channels]

    random.seed(args.seed); np.random.seed(args.seed); torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)
    torch.use_deterministic_algorithms(True)
    if torch.backends.cudnn.is_available():
        torch.backends.cudnn.benchmark = False
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    runs, skipped = load_runs(args.data, pressure_columns)
    train_runs, val_runs, test_runs = split_from_manifest(runs, args.split_manifest)
    train = prepare_runs(train_runs, args.window_samples, args.train_stride)
    validation_train_stride = prepare_runs(val_runs, args.window_samples, args.train_stride)
    val_replay_runs = prepare_runs(val_runs, args.window_samples, args.replay_stride)
    test_replay_runs = prepare_runs(test_runs, args.window_samples, args.replay_stride)
    mean, std = normalization(train)
    model = CausalPressureTCN(mean, std).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=1e-4)

    best_state = None
    best_val = math.inf
    best_epoch = 0
    stale = 0
    rng = random.Random(args.seed)
    for epoch in range(1, args.epochs + 1):
        model.train()
        shuffled = train[:]
        rng.shuffle(shuffled)
        train_losses = []
        for start in range(0, len(shuffled), args.run_batch_size):
            group = shuffled[start:start + args.run_batch_size]
            optimizer.zero_grad(set_to_none=True)
            loss = mil_loss(model, group, device, True, args.top_k, args.negative_aux)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 3.0)
            optimizer.step()
            train_losses.append(float(loss.detach().cpu()))
        model.eval()
        with torch.no_grad():
            val_loss = float(mil_loss(model, validation_train_stride, device, False,
                                      args.top_k, args.negative_aux).cpu())
        if val_loss < best_val - 1e-4:
            best_val, best_epoch, stale = val_loss, epoch, 0
            best_state = {name: value.detach().cpu().clone() for name, value in model.state_dict().items()}
        else:
            stale += 1
        if epoch == 1 or epoch % 10 == 0:
            print(f"epoch={epoch:03d} train_loss={np.mean(train_losses):.5f} val_loss={val_loss:.5f}", flush=True)
        if stale >= args.patience:
            break
    if best_state is None:
        raise RuntimeError("training did not produce a checkpoint")
    model.load_state_dict(best_state)
    model.to(device).eval()

    validation_replay = replay(model, val_replay_runs, device, args.inference_batch_size)
    threshold, validation_metrics = choose_threshold(validation_replay, args.consecutive_hits)
    test_replay = replay(model, test_replay_runs, device, args.inference_batch_size)
    test_metrics = run_metrics(test_replay, threshold, args.consecutive_hits)

    args.output.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "model_state": best_state, "feature_mean": mean, "feature_std": std,
        "window_samples": args.window_samples, "channels": channels, "architecture": "causal_tcn",
        "probability_threshold": threshold, "consecutive_hits": args.consecutive_hits,
    }
    torch.save(checkpoint, args.output / "pressure_causal_tcn.pt")
    dummy = torch.zeros(1, len(channels), 1, args.window_samples, device=device)
    torch.onnx.export(
        model, dummy, args.output / "pressure_causal_tcn.onnx",
        input_names=["pressure"], output_names=["probability_logit"], opset_version=13,
        dynamic_axes=None, dynamo=False,
    )

    with (args.output / "test_predictions.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        rows = prediction_rows(test_replay, threshold, args.consecutive_hits)
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    source_manifest = json.loads(args.split_manifest.read_text(encoding="utf-8"))
    manifest = {
        "schema_version": 2,
        "model": "strictly causal TCN trained with run-level multiple-instance learning",
        "input_shape": [1, len(channels), 1, args.window_samples],
        "output_shape": [1, 1, 1, 1],
        "pressure_columns": pressure_columns,
        "sample_rate_hz": 200,
        "probability_threshold": threshold,
        "consecutive_hits": args.consecutive_hits,
        "window_ms": int(args.window_samples / 200 * 1000),
        "decision_policy": "alarm after consecutive threshold crossings during forward motion",
        "causality": "model input contains only the current and preceding 199 samples",
        "training_region": "forward-run samples before the configured deceleration distance; stop/fall excluded",
        "labeling": "one label per complete run; no manually selected positive time interval",
        "training_batches": source_manifest["training_batches"],
        "validation_batches": source_manifest["validation_batches"],
        "test_batches": source_manifest["test_batches"],
        "split_run_counts": {"training": len(train), "validation": len(val_replay_runs), "test": len(test_replay_runs)},
        "training_hyperparameters": {
            "seed": args.seed, "best_epoch": best_epoch, "max_epochs": args.epochs,
            "learning_rate": args.learning_rate, "run_batch_size": args.run_batch_size,
            "train_stride_samples": args.train_stride, "top_k_windows": args.top_k,
            "negative_window_regularization": args.negative_aux,
            "augmentation": "per-channel gain, offset, linear drift and Gaussian noise",
        },
        "architecture": "10-feature causal TCN, width 24, residual dilations 1/2/4/8/16, fixed causal endpoint head",
        "parameter_count": sum(parameter.numel() for parameter in model.parameters()),
        "preprocessing": {
            "features": ["window-relative pressure", "first difference"],
            "feature_mean": mean.tolist(), "feature_std": std.tolist(),
            "normalization_embedded_in_model": True,
        },
        "validation_metrics": validation_metrics,
        "test_metrics": test_metrics,
        "excluded_trials": len(skipped),
        "deployment_note": "Atlas must preserve a rolling 5x200 buffer and apply consecutive_hits exactly as recorded here.",
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "deployment_config.json").write_text(json.dumps({
        "ascend_om": str((args.output / "pressure_causal_tcn.om").as_posix()),
        "window_samples": args.window_samples, "channels": channels,
        "probability_threshold": threshold, "consecutive_hits": args.consecutive_hits,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    old_test = source_manifest.get("test_metrics", {}).get("run", {})
    old_accuracy = float(old_test.get("accuracy", (old_test.get("tp", 0) + old_test.get("tn", 0)) /
                         max(1, old_test.get("runs", 0))))
    accuracy_gain = test_metrics["accuracy"] - old_accuracy
    fp_change = int(test_metrics["fp"]) - int(old_test.get("fp", 0))
    fn_change = int(test_metrics["fn"]) - int(old_test.get("fn", 0))
    report = f"""# 实时因果模型训练报告

- 模型：{len(channels)}通道因果 TCN（传感器 {','.join(map(str, channels))}，严格只看当前与历史数据）
- 输入：`1 × {len(channels)} × 1 × {args.window_samples}`，约 {args.window_samples / 200:.1f} 秒历史数据
- 训练方式：整次实验弱监督，多实例学习（MIL）
- 终点处理：减速区及终点急降完全排除
- 最佳轮次：{best_epoch}
- 参数量：{manifest['parameter_count']:,}
- 报警规则：概率 ≥ {threshold:.2f}，连续 {args.consecutive_hits} 个采样点命中

## 验证集（仅用于选择阈值）

- 准确率：{validation_metrics['accuracy']:.2%}
- 平衡准确率：{validation_metrics['balanced_accuracy']:.2%}
- 召回率：{validation_metrics['recall']:.2%}
- 特异度：{validation_metrics['specificity']:.2%}
- TP/FP/TN/FN：{validation_metrics['tp']}/{validation_metrics['fp']}/{validation_metrics['tn']}/{validation_metrics['fn']}

## 独立测试集

- 准确率：{test_metrics['accuracy']:.2%}
- 平衡准确率：{test_metrics['balanced_accuracy']:.2%}
- 召回率：{test_metrics['recall']:.2%}
- 特异度：{test_metrics['specificity']:.2%}
- F1：{test_metrics['f1']:.2%}
- TP/FP/TN/FN：{test_metrics['tp']}/{test_metrics['fp']}/{test_metrics['tn']}/{test_metrics['fn']}
- 检出样本距减速区的中位提前量：{test_metrics['detected_positive_lead_mm_median']} mm

## 与上一版对照

- 上一版测试准确率：{old_accuracy:.2%}
- 当前测试准确率：{test_metrics['accuracy']:.2%}
- 变化：{accuracy_gain:+.2%}（误报变化 {fp_change:+d} 次，漏检变化 {fn_change:+d} 次）

这组结果按完整 CSV 批次隔离，测试集没有参与训练或阈值选择。上线时必须采用相同的连续命中规则。
"""
    (args.output / "TRAINING_REPORT.md").write_text(report, encoding="utf-8")
    print(json.dumps({"device": str(device), "best_epoch": best_epoch,
                      "validation": validation_metrics, "test": test_metrics}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
