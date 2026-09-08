from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from compare_20260829 import GRID, curve_features, fit_lda, moving_average, predict_lda


FILES = [
    Path(r"D:\聊天文件\微信\xwechat_files\wxid_1dpbeiucwjug22_2a10\msg\file\2026-08\20260829T092929_batch_001.csv"),
    Path(r"D:\聊天文件\微信\xwechat_files\wxid_1dpbeiucwjug22_2a10\msg\file\2026-08\20260829T093114_batch_001.csv"),
]


def resample(frame: pd.DataFrame) -> np.ndarray:
    d = frame.loc[
        (frame["phase"] == "measure")
        & frame["position_mm"].notna()
        & frame["pressure_pa_1"].notna()
        & frame["valid_mask"].astype(int).map(lambda value: bool(value & 1)),
        ["position_mm", "pressure_pa_1", "esp_uptime_ms"],
    ].sort_values("esp_uptime_ms")
    d = d.groupby("position_mm", as_index=False)["pressure_pa_1"].mean().sort_values("position_mm")
    return np.interp(GRID, d["position_mm"].to_numpy(float), d["pressure_pa_1"].to_numpy(float))


def features_on_grid(curve: np.ndarray, grid: np.ndarray) -> np.ndarray:
    grad = np.gradient(curve, grid)
    feats = [
        np.mean(curve), np.std(curve), np.min(curve), np.max(curve), np.ptp(curve),
        np.quantile(curve, 0.1), np.quantile(curve, 0.9),
        np.max(grad), np.min(grad), np.std(grad),
    ]
    for idx in np.array_split(np.arange(curve.size), 12):
        feats.extend((np.mean(curve[idx]), np.std(curve[idx]), np.max(curve[idx])))
    return np.asarray(feats, float)


def paired_leave_out(curves0: np.ndarray, curves1: np.ndarray, grid: np.ndarray = GRID):
    curves = np.vstack((curves0, curves1))
    y = np.r_[np.zeros(len(curves0), int), np.ones(len(curves1), int)]
    x = np.vstack([features_on_grid(curve, grid) for curve in curves])
    predictions, truths = [], []
    for i in range(min(len(curves0), len(curves1))):
        test = np.asarray([i, len(curves0) + i])
        train = np.setdiff1d(np.arange(len(curves)), test)
        pred = predict_lda(fit_lda(x[train], y[train]), x[test])
        predictions.extend(pred.tolist())
        truths.extend(y[test].tolist())
    predictions = np.asarray(predictions)
    truths = np.asarray(truths)
    return float(np.mean(predictions == truths)), int(np.sum(predictions == truths)), len(truths)


def main():
    report = {"files": {}, "comparison": {}}
    curve_sets = []
    for path in FILES:
        df = pd.read_csv(path)
        raw = np.vstack([resample(trial) for _, trial in df.groupby("trial_id", sort=True)])
        filtered = np.vstack([moving_average(curve, 5) for curve in raw])
        curve_sets.append((raw, filtered))
        mean = filtered.mean(axis=0)
        correlations = [np.corrcoef(curve, mean)[0, 1] for curve in filtered]
        valid = df["valid_mask"].astype(int).map(lambda value: bool(value & 1))
        dt = df.groupby("trial_id")["esp_uptime_ms"].diff().dropna()
        report["files"][path.name] = {
            "rows": len(df),
            "trials": int(df["trial_id"].nunique()),
            "channel_1_valid_fraction": float(valid.mean()),
            "other_channels_nonzero_rows": int((df[[f"pressure_pa_{i}" for i in range(2, 6)]].abs().sum(axis=1) > 0).sum()),
            "median_sample_interval_ms": float(dt.median()),
            "approx_sample_rate_hz": float(1000 / dt.median()),
            "raw_pressure_range_pa": [float(df["pressure_pa_1"].min()), float(df["pressure_pa_1"].max())],
            "raw_abs_over_3pa_count": int((df["pressure_pa_1"].abs() > 3).sum()),
            "mean_curve_range_pa": [float(mean.min()), float(mean.max())],
            "mean_curve_peak_distance_mm": float(GRID[int(np.argmax(mean))]),
            "mean_across_distance_trial_sd_pa": float(filtered.std(axis=0, ddof=1).mean()),
            "median_trial_to_mean_correlation": float(np.median(correlations)),
        }

    for curve_name, index in (("raw", 0), ("filtered_5pt", 1)):
        a, b = curve_sets[0][index], curve_sets[1][index]
        gap = b.mean(axis=0) - a.mean(axis=0)
        pooled = np.sqrt(((len(a) - 1) * a.var(axis=0, ddof=1) + (len(b) - 1) * b.var(axis=0, ddof=1)) / (len(a) + len(b) - 2))
        effect = gap / np.maximum(pooled, 1e-9)
        gap_i = int(np.argmax(np.abs(gap)))
        effect_i = int(np.argmax(np.abs(effect)))
        accuracy, correct, total = paired_leave_out(a, b)
        report["comparison"][curve_name] = {
            "rms_mean_curve_gap_pa": float(np.sqrt(np.mean(gap ** 2))),
            "max_abs_mean_gap_pa": float(abs(gap[gap_i])),
            "max_gap_distance_mm": float(GRID[gap_i]),
            "median_abs_cohen_d": float(np.median(np.abs(effect))),
            "max_abs_cohen_d": float(abs(effect[effect_i])),
            "max_effect_distance_mm": float(GRID[effect_i]),
            "batch_separability_accuracy": accuracy,
            "correct": correct,
            "total": total,
        }

    preterminal = GRID <= 1700
    a = curve_sets[0][1][:, preterminal]
    b = curve_sets[1][1][:, preterminal]
    accuracy, correct, total = paired_leave_out(a, b, GRID[preterminal])
    report["comparison"]["filtered_5pt_distance_20_1700mm"] = {
        "batch_separability_accuracy": accuracy,
        "correct": correct,
        "total": total,
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
