from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


FILES = [
    (Path(r"C:\Users\Rylan\Downloads\20260829T082017_batch_001.csv"), "fan", 1),
    (Path(r"C:\Users\Rylan\Downloads\20260829T082258_batch_001.csv"), "fan", 0),
    (Path(r"C:\Users\Rylan\Downloads\20260829T081511_batch_001.csv"), "no_fan", 1),
    (Path(r"C:\Users\Rylan\Downloads\20260829T081204_batch_001.csv"), "no_fan", 0),
]

GRID = np.arange(20.0, 1880.1, 10.0)


def moving_average(x: np.ndarray, width: int = 5) -> np.ndarray:
    if width <= 1:
        return x.copy()
    left = width // 2
    right = width - 1 - left
    xp = np.pad(x, (left, right), mode="edge")
    return np.convolve(xp, np.ones(width) / width, mode="valid")


def resample_trial(frame: pd.DataFrame) -> np.ndarray:
    d = frame.loc[
        (frame["phase"] == "measure")
        & (frame["valid_mask"].astype(int).map(lambda v: bool(v & 1)))
        & frame["position_mm"].notna()
        & frame["pressure_pa_1"].notna(),
        ["position_mm", "pressure_pa_1", "esp_uptime_ms"],
    ].copy()
    d = d.sort_values("esp_uptime_ms")
    # Position may repeat during acceleration/stops. Mean only exact duplicate positions,
    # then interpolate onto a fixed distance grid so every trial contributes equally.
    d = d.groupby("position_mm", as_index=False)["pressure_pa_1"].mean().sort_values("position_mm")
    x = d["position_mm"].to_numpy(float)
    y = d["pressure_pa_1"].to_numpy(float)
    keep = np.r_[True, np.diff(x) > 0]
    return np.interp(GRID, x[keep], y[keep])


def load_trials():
    rows = []
    for path, wind, label in FILES:
        df = pd.read_csv(path)
        for trial_id, trial in df.groupby("trial_id", sort=True):
            raw = resample_trial(trial)
            rows.append(
                {
                    "file": path.name,
                    "wind": wind,
                    "label": label,
                    "trial_id": trial_id,
                    "curve_raw": raw,
                    "curve_filtered": moving_average(raw, 5),
                }
            )
    return rows


def curve_features(curve: np.ndarray) -> np.ndarray:
    # Fixed, label-independent feature map. Retains spike/ramp evidence and broad shape.
    grad = np.gradient(curve, GRID)
    feats = [
        np.mean(curve), np.std(curve), np.min(curve), np.max(curve),
        np.ptp(curve), np.quantile(curve, 0.1), np.quantile(curve, 0.9),
        np.max(grad), np.min(grad), np.std(grad),
    ]
    for idx in np.array_split(np.arange(curve.size), 12):
        feats.extend((np.mean(curve[idx]), np.std(curve[idx]), np.max(curve[idx])))
    return np.asarray(feats, float)


def fit_lda(x: np.ndarray, y: np.ndarray):
    mean = x.mean(axis=0)
    scale = x.std(axis=0, ddof=1)
    scale[scale < 1e-8] = 1.0
    z = (x - mean) / scale
    m0 = z[y == 0].mean(axis=0)
    m1 = z[y == 1].mean(axis=0)
    centered = np.vstack((z[y == 0] - m0, z[y == 1] - m1))
    cov = centered.T @ centered / max(1, len(z) - 2)
    # Strong shrinkage is deliberate because n=38 train trials and 46 features.
    diag = np.diag(np.diag(cov))
    cov = 0.25 * cov + 0.75 * diag + np.eye(cov.shape[0]) * 0.25
    inv = np.linalg.pinv(cov)
    w = inv @ (m1 - m0)
    threshold = 0.5 * float((m1 + m0) @ w)
    return mean, scale, w, threshold


def predict_lda(model, x: np.ndarray) -> np.ndarray:
    mean, scale, w, threshold = model
    return (((x - mean) / scale) @ w >= threshold).astype(int)


def leave_pair_out_accuracy(rows, curve_key: str, train_wind: str, test_wind: str | None = None):
    train_rows = [r for r in rows if r["wind"] == train_wind]
    test_rows = [r for r in rows if r["wind"] == (test_wind or train_wind)]
    x_train_all = np.vstack([curve_features(r[curve_key]) for r in train_rows])
    y_train_all = np.asarray([r["label"] for r in train_rows])
    x_test_all = np.vstack([curve_features(r[curve_key]) for r in test_rows])
    y_test_all = np.asarray([r["label"] for r in test_rows])

    if test_wind is not None:
        pred = predict_lda(fit_lda(x_train_all, y_train_all), x_test_all)
        return float(np.mean(pred == y_test_all)), pred, y_test_all

    # Trial indices are paired across obstacle/no-obstacle recordings. Hold out the
    # same repetition number from both classes to avoid near-neighbor leakage.
    ids0 = np.flatnonzero(y_train_all == 0)
    ids1 = np.flatnonzero(y_train_all == 1)
    correct = []
    pred_all = []
    truth_all = []
    for a, b in zip(ids0, ids1):
        test = np.asarray([a, b])
        train = np.setdiff1d(np.arange(len(train_rows)), test)
        pred = predict_lda(fit_lda(x_train_all[train], y_train_all[train]), x_train_all[test])
        correct.extend((pred == y_train_all[test]).tolist())
        pred_all.extend(pred.tolist())
        truth_all.extend(y_train_all[test].tolist())
    return float(np.mean(correct)), np.asarray(pred_all), np.asarray(truth_all)


def combined_leave_repetition_out(rows, curve_key: str):
    x = np.vstack([curve_features(r[curve_key]) for r in rows])
    y = np.asarray([r["label"] for r in rows])
    # Each batch has 20 repetitions in sorted trial-id order. Hold out the same
    # repetition from all four batches, leaving four test trials per fold.
    batch_groups = {}
    for idx, row in enumerate(rows):
        batch_groups.setdefault(row["file"], []).append(idx)
    ordered = [batch_groups[path.name] for path, _, _ in FILES]
    pred_all, truth_all = [], []
    for repetition in range(min(map(len, ordered))):
        test = np.asarray([group[repetition] for group in ordered])
        train = np.setdiff1d(np.arange(len(rows)), test)
        pred = predict_lda(fit_lda(x[train], y[train]), x[test])
        pred_all.extend(pred.tolist())
        truth_all.extend(y[test].tolist())
    pred_all = np.asarray(pred_all)
    truth_all = np.asarray(truth_all)
    return float(np.mean(pred_all == truth_all)), pred_all, truth_all


def binomial_ci(k: int, n: int):
    # Wilson 95% interval.
    z = 1.959963984540054
    p = k / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [float(center - half), float(center + half)]


def difference_metrics(rows, wind: str, curve_key: str):
    a = np.vstack([r[curve_key] for r in rows if r["wind"] == wind and r["label"] == 1])
    b = np.vstack([r[curve_key] for r in rows if r["wind"] == wind and r["label"] == 0])
    gap = a.mean(axis=0) - b.mean(axis=0)
    pooled = np.sqrt(((len(a) - 1) * a.var(axis=0, ddof=1) + (len(b) - 1) * b.var(axis=0, ddof=1)) / (len(a) + len(b) - 2))
    effect = gap / np.maximum(pooled, 1e-9)
    idx_gap = int(np.argmax(np.abs(gap)))
    idx_effect = int(np.argmax(np.abs(effect)))
    return {
        "n_obstacle": len(a),
        "n_clear": len(b),
        "rms_mean_curve_gap_pa": float(np.sqrt(np.mean(gap ** 2))),
        "max_abs_mean_gap_pa": float(abs(gap[idx_gap])),
        "max_gap_distance_mm": float(GRID[idx_gap]),
        "median_abs_cohen_d": float(np.median(np.abs(effect))),
        "max_abs_cohen_d": float(abs(effect[idx_effect])),
        "max_effect_distance_mm": float(GRID[idx_effect]),
        "obstacle_peak_range_pa": [float(a.min()), float(a.max())],
        "clear_peak_range_pa": [float(b.min()), float(b.max())],
    }


def main():
    rows = load_trials()
    report = {
        "grid": {"start_mm": float(GRID[0]), "end_mm": float(GRID[-1]), "step_mm": float(GRID[1] - GRID[0])},
        "trial_counts": {},
        "difference": {},
        "classification": {},
    }
    for wind in ("fan", "no_fan"):
        report["trial_counts"][wind] = {
            "obstacle": sum(r["wind"] == wind and r["label"] == 1 for r in rows),
            "clear": sum(r["wind"] == wind and r["label"] == 0 for r in rows),
        }
        for curve_key in ("curve_raw", "curve_filtered"):
            report["difference"][f"{wind}_{curve_key}"] = difference_metrics(rows, wind, curve_key)
            acc, pred, truth = leave_pair_out_accuracy(rows, curve_key, wind)
            k = int(np.sum(pred == truth))
            report["classification"][f"{wind}_{curve_key}"] = {
                "accuracy": acc,
                "correct": k,
                "total": len(truth),
                "wilson_95_ci": binomial_ci(k, len(truth)),
                "sensitivity": float(np.mean(pred[truth == 1] == 1)),
                "specificity": float(np.mean(pred[truth == 0] == 0)),
            }

    for train_wind, test_wind in (("fan", "no_fan"), ("no_fan", "fan")):
        for curve_key in ("curve_raw", "curve_filtered"):
            acc, pred, truth = leave_pair_out_accuracy(rows, curve_key, train_wind, test_wind)
            report["classification"][f"train_{train_wind}_test_{test_wind}_{curve_key}"] = {
                "accuracy": acc,
                "sensitivity": float(np.mean(pred[truth == 1] == 1)),
                "specificity": float(np.mean(pred[truth == 0] == 0)),
            }

    for curve_key in ("curve_raw", "curve_filtered"):
        acc, pred, truth = combined_leave_repetition_out(rows, curve_key)
        k = int(np.sum(pred == truth))
        report["classification"][f"combined_{curve_key}"] = {
            "accuracy": acc,
            "correct": k,
            "total": len(truth),
            "wilson_95_ci": binomial_ci(k, len(truth)),
            "sensitivity": float(np.mean(pred[truth == 1] == 1)),
            "specificity": float(np.mean(pred[truth == 0] == 0)),
        }

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
