#!/usr/bin/env python3
"""Train a causal single-channel pressure-ramp classifier from ESP32 UI CSV files."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import random
import torch
import numpy as np

from torch import nn
from torch.utils.data import DataLoader, TensorDataset


PRESSURE_COLUMNS = ["pressure_pa_1"]


def load_runs(path: Path) -> list[dict]:
    """Load one CSV batch and split it into independent forward measurement trials."""
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or any(column not in rows[0] for column in PRESSURE_COLUMNS):
        return []
    measurement_rows = [row for row in rows if not row.get("phase") or row.get("phase") == "measure"]
    groups: dict[str, list[dict]] = {}
    for row in measurement_rows:
        trial_id = row.get("trial_id") or row.get("run_id") or path.stem
        groups.setdefault(trial_id, []).append(row)
    result = []
    for trial_id, trial_rows in groups.items():
        if int(float(trial_rows[0].get("sensor_test_mode", "0") or 0)):
            print(f"skip {path.name}/{trial_id}: ESP32 test data")
            continue
        try:
            pressure = np.asarray(
                [[float(row[column]) for column in PRESSURE_COLUMNS] for row in trial_rows],
                dtype=np.float32,
            ).T
            moving = np.asarray([int(float(row.get("moving", "1") or 0))
                                 for row in trial_rows], dtype=np.int8)
            valid_mask = np.asarray(
                [int(float(row.get("valid_mask", "31") or 0)) for row in trial_rows]
            )
        except (TypeError, ValueError):
            print(f"skip {path.name}/{trial_id}: invalid numeric value")
            continue
        if pressure.shape[1] < 20:
            print(f"skip {path.name}/{trial_id}: fewer than 20 samples")
            continue
        required_mask = (1 << len(PRESSURE_COLUMNS)) - 1
        if np.mean((valid_mask & required_mask) == required_mask) < 0.9:
            print(f"skip {path.name}/{trial_id}: channel 1 is valid in fewer than 90% of samples")
            continue
        usable = True
        num_channels = len(PRESSURE_COLUMNS)
        for channel in range(num_channels):
            valid = (valid_mask & (1 << channel)) != 0
            indices = np.arange(pressure.shape[1])
            if not np.any(valid):
                usable = False
                break
            pressure[channel] = np.interp(indices, indices[valid], pressure[channel, valid])
        if usable:
            result.append({
                "path": path,
                "batch": path.resolve(),
                "name": trial_id,
                "condition": trial_rows[0].get("condition", ""),
                "label": int(float(trial_rows[0].get("label", "0") or 0)),
                "pressure": pressure,
                "moving": moving,
            })
    return result


def terminal_drop_index(pressure: np.ndarray, moving: np.ndarray | None = None) -> int:
    """Find the terminal fall near the recorded moving-to-stopped transition."""
    length = pressure.shape[1]
    if length < 20:
        return length - 1
    smooth_width = max(3, min(11, length // 50 * 2 + 1))
    kernel = np.ones(smooth_width, dtype=np.float32) / smooth_width
    mean_pressure = np.mean(pressure, axis=0)
    smooth = np.convolve(mean_pressure, kernel, mode="same")
    derivative = np.diff(smooth, prepend=smooth[0])
    transitions = (np.flatnonzero((moving[:-1] == 1) & (moving[1:] == 0)) + 1
                   if moving is not None and len(moving) == length else np.asarray([]))
    if len(transitions):
        motion_stop = int(transitions[-1])
        start = max(2, int(length * 0.45), motion_stop - 70)
        stop = min(length - 2, motion_stop + 5)
    else:
        start, stop = max(2, int(length * 0.65)), max(3, int(length * 0.95))
    if stop <= start:
        return length - 1
    return start + int(np.argmin(derivative[start:stop]))


def windows_for_run(run: dict, window: int, stride: int, positive_horizon: int,
                    guard: int) -> tuple[np.ndarray, np.ndarray]:
    pressure = run["pressure"]
    drop = terminal_drop_index(pressure, run.get("moving"))
    target_end = max(window, drop - guard)
    ends = list(range(window, target_end + 1, stride))
    if not ends or ends[-1] != target_end:
        ends.append(target_end)
    samples, labels = [], []
    for end in ends:
        samples.append(pressure[:, end - window:end])
        is_ramp = run["label"] == 1 and end >= target_end - positive_horizon
        labels.append(float(is_ramp))
    return np.stack(samples), np.asarray(labels, dtype=np.float32)


def split_runs(runs: list[dict], seed: int) -> tuple[list[dict], list[dict], list[dict]]:
    """Split whole CSV batches, never sibling trials from the same batch."""
    rng = random.Random(seed)
    batch_buckets: dict[int, dict[Path, list[dict]]] = {0: {}, 1: {}}
    for run in runs:
        batch_buckets[run["label"]].setdefault(run["batch"], []).append(run)
    result = [[], [], []]
    for label, batch_map in batch_buckets.items():
        batches = list(batch_map.values())
        rng.shuffle(batches)
        if len(batches) < 3:
            raise ValueError(
                f"label {label} needs at least 3 separate batch CSV files "
                "to prevent train/validation/test leakage"
            )
        train_count = max(1, int(len(batches) * 0.7))
        val_count = max(1, int(len(batches) * 0.15))
        if train_count + val_count >= len(batches):
            train_count, val_count = len(batches) - 2, 1
        parts = (
            batches[:train_count],
            batches[train_count:train_count + val_count],
            batches[train_count + val_count:],
        )
        for index, part in enumerate(parts):
            result[index].extend(run for one_batch in part for run in one_batch)
    for part in result:
        rng.shuffle(part)
    return result[0], result[1], result[2]


def split_named(runs: list[dict], seed: int) -> tuple[list[dict], list[dict], list[dict]]:
    """Use *_t.csv for training, *_v.csv for validation and *_test.csv for final test."""
    result = [[], [], []]
    for run in runs:
        stem = run["path"].stem.lower()
        if stem.endswith("_test"):
            result[2].append(run)
        elif stem.endswith("_v"):
            result[1].append(run)
        elif stem.endswith("_t"):
            result[0].append(run)
    for name, part in zip(("training (*_t.csv)", "validation (*_v.csv)"), result[:2]):
        if {run["label"] for run in part} != {0, 1}:
            raise ValueError(f"{name} must contain both label 0 and label 1")
    rng = random.Random(seed)
    for part in result:
        rng.shuffle(part)
    return result[0], result[1], result[2]


def make_split(runs: list[dict], args) -> tuple[torch.Tensor, torch.Tensor]:
    samples, labels = [], []
    for run in runs:
        x, y = windows_for_run(run, args.window_samples, args.stride_samples,
                               args.positive_horizon_samples, args.drop_guard_samples)
        samples.append(x)
        labels.append(y)
    return torch.from_numpy(np.concatenate(samples)[:, :, None, :]), torch.from_numpy(np.concatenate(labels))


class RampNet(nn.Module):
    def __init__(self, feature_mean: torch.Tensor, feature_std: torch.Tensor):
        super().__init__()
        feature_count = int(feature_mean.numel())
        self.register_buffer("feature_mean", feature_mean.reshape(1, feature_count, 1, 1))
        self.register_buffer("feature_std", feature_std.reshape(1, feature_count, 1, 1))
        self.features = nn.Sequential(
            nn.Conv2d(feature_count, 16, kernel_size=(1, 7), padding=(0, 3)), nn.ReLU(),
            nn.Conv2d(16, 32, kernel_size=(1, 5), padding=(0, 4), dilation=(1, 2)), nn.ReLU(),
            nn.Conv2d(32, 32, kernel_size=(1, 3), padding=(0, 2), dilation=(1, 2)), nn.ReLU(),
            nn.AdaptiveAvgPool2d((1, 1)),
        )
        self.classifier = nn.Linear(32, 1)

    @staticmethod
    def derive(raw: torch.Tensor) -> torch.Tensor:
        centered = raw - raw[..., :1]
        delta = torch.cat((torch.zeros_like(raw[..., :1]), raw[..., 1:] - raw[..., :-1]), dim=-1)
        return torch.cat((centered, delta), dim=1)

    def forward(self, raw: torch.Tensor) -> torch.Tensor:
        value = (self.derive(raw) - self.feature_mean) / self.feature_std
        return self.classifier(self.features(value).flatten(1)).squeeze(1)


def metrics(probability: np.ndarray, target: np.ndarray, threshold: float) -> dict:
    predicted = probability >= threshold
    truth = target >= 0.5
    tp = int(np.sum(predicted & truth)); fp = int(np.sum(predicted & ~truth))
    tn = int(np.sum(~predicted & ~truth)); fn = int(np.sum(~predicted & truth))
    precision = tp / max(1, tp + fp); recall = tp / max(1, tp + fn)
    specificity = tn / max(1, tn + fp)
    return {"threshold": threshold, "tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "precision": precision, "recall": recall, "specificity": specificity,
            "balanced_accuracy": (recall + specificity) / 2,
            "f1": 2 * precision * recall / max(1e-12, precision + recall)}


@torch.no_grad()
def probabilities(model: nn.Module, x: torch.Tensor, batch_size: int) -> np.ndarray:
    model.eval()
    chunks = [torch.sigmoid(model(x[index:index + batch_size])).cpu() for index in range(0, len(x), batch_size)]
    return torch.cat(chunks).numpy()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path, nargs="?",
                        default=Path(r"E:\Research\2026-8-22-pressure"),
                        help="directory containing ESP32 CSV files (default: E:\\Research\\2026-8-22-pressure)")
    parser.add_argument("--output", type=Path, default=Path("training/output"))
    parser.add_argument("--sample-rate-hz", type=int, default=200)
    parser.add_argument("--window-samples", type=int, default=200, help="causal input length; 200=1 s at 200 Hz")
    parser.add_argument("--stride-samples", type=int, default=20)
    parser.add_argument("--positive-horizon-samples", type=int, default=60, help="ramp region before terminal fall")
    parser.add_argument("--drop-guard-samples", type=int, default=6, help="exclude samples immediately next to fall")
    parser.add_argument("--epochs", type=int, default=80)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--seed", type=int, default=20260821)
    parser.add_argument("--split-mode", choices=("auto", "named", "batch"), default="auto",
                        help="auto uses *_t/*_v names when present, otherwise whole-batch split")
    args = parser.parse_args()
    random.seed(args.seed); np.random.seed(args.seed); torch.manual_seed(args.seed)
    if args.window_samples < 20 or args.positive_horizon_samples < 1:
        raise ValueError("window and positive horizon are too short")

    runs = [run for path in sorted(args.data.rglob("*.csv")) for run in load_runs(path)]
    if not runs:
        raise ValueError("no usable ESP32 CSV files found")
    has_named = any(run["path"].stem.lower().endswith("_t") for run in runs) and \
                any(run["path"].stem.lower().endswith("_v") for run in runs)
    split_mode = "named" if args.split_mode == "auto" and has_named else args.split_mode
    if split_mode == "auto":
        split_mode = "batch"
    train_runs, val_runs, test_runs = (split_named(runs, args.seed) if split_mode == "named"
                                       else split_runs(runs, args.seed))
    train_x, train_y = make_split(train_runs, args)
    val_x, val_y = make_split(val_runs, args)
    test_x, test_y = make_split(test_runs, args) if test_runs else (None, None)

    derived = RampNet.derive(train_x)
    feature_mean = derived.mean(dim=(0, 2, 3))
    feature_std = derived.std(dim=(0, 2, 3)).clamp_min(1e-5)
    model = RampNet(feature_mean, feature_std)
    positives = float(train_y.sum())
    positive_weight = torch.tensor([(len(train_y) - positives) / max(1.0, positives)])
    loss_fn = nn.BCEWithLogitsLoss(pos_weight=positive_weight)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=1e-4)
    loader = DataLoader(TensorDataset(train_x, train_y), batch_size=args.batch_size, shuffle=True)
    best_loss = math.inf
    best_state = None
    for epoch in range(args.epochs):
        model.train()
        for batch_x, batch_y in loader:
            optimizer.zero_grad()
            loss = loss_fn(model(batch_x), batch_y)
            loss.backward()
            optimizer.step()
        with torch.no_grad():
            validation_loss = float(loss_fn(model(val_x), val_y))
        if validation_loss < best_loss:
            best_loss = validation_loss
            best_state = {key: value.detach().clone() for key, value in model.state_dict().items()}
        if epoch == 0 or (epoch + 1) % 10 == 0:
            print(f"epoch {epoch + 1:03d} validation_loss={validation_loss:.6f}")
    model.load_state_dict(best_state)

    val_probability = probabilities(model, val_x, args.batch_size)
    choices = [metrics(val_probability, val_y.numpy(), threshold / 100) for threshold in range(5, 96)]
    selected = max(choices, key=lambda value: (value["balanced_accuracy"], value["f1"]))
    test_result = (metrics(probabilities(model, test_x, args.batch_size), test_y.numpy(),
                           selected["threshold"]) if test_x is not None else None)
    args.output.mkdir(parents=True, exist_ok=True)
    torch.save({"state_dict": model.state_dict(), "window_samples": args.window_samples,
                "threshold": selected["threshold"]}, args.output / "pressure_ramp.pt")
    model.eval()
    dummy = torch.zeros(1, len(PRESSURE_COLUMNS), 1, args.window_samples)
    onnx_path = args.output / "pressure_ramp.onnx"
    try:
        torch.onnx.export(model, dummy, onnx_path, input_names=["pressure"],
                          output_names=["logit"], opset_version=13,
                          dynamo=False, external_data=False)
        print(f"ONNX model saved to {onnx_path}")
    except Exception as e:
        print(f"Warning: ONNX export failed ({e}). Install onnxscript or onnx to enable it.")
    manifest = {
        "schema_version": 1,
        "input_shape": [1, len(PRESSURE_COLUMNS), 1, args.window_samples],
        "pressure_columns": PRESSURE_COLUMNS,
        "split_mode": split_mode,
        "sample_rate_hz": args.sample_rate_hz,
        "probability_threshold": selected["threshold"],
        "window_ms": round(args.window_samples * 1000 / args.sample_rate_hz),
        "training_runs": [run["name"] for run in train_runs],
        "validation_runs": [run["name"] for run in val_runs],
        "test_runs": [run["name"] for run in test_runs],
        "validation_metrics": selected,
        "test_metrics": test_result,
        "warning": ("No independent *_test.csv files were supplied; validation metrics were used "
                    "for threshold selection and are not final test metrics. "
                    "Window metrics are not a substitute for false alarms per complete scan."
                    if test_result is None else
                    "Window metrics are not a substitute for false alarms per complete scan."),
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "test": test_result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
