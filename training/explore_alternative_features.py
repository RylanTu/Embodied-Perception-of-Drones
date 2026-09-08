#!/usr/bin/env python3
"""Explore causal, pre-drop alternatives beyond a single pressure ramp feature."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import statistics

import numpy as np

from analyze_discriminability import auc, kalman, load_trials, terminal_drop


def slope(values: np.ndarray, rate: float = 200.0) -> float:
    x = np.arange(len(values), dtype=float)
    x -= x.mean()
    denominator = float(x @ x)
    return float(x @ values / denominator * rate) if denominator else 0.0


def fixed_curve(trial: dict, samples: int = 250, guard: int = 10) -> np.ndarray | None:
    filtered = np.asarray(kalman(trial["pressure"]), dtype=float)
    drop = terminal_drop(filtered.tolist(), trial["moving"])
    end = drop - guard
    if end < samples:
        return None
    curve = filtered[end - samples:end].copy()
    curve -= np.median(curve[:20])
    return curve


def motion_start_curve(trial: dict, samples: int = 250) -> np.ndarray | None:
    filtered = np.asarray(kalman(trial["pressure"]), dtype=float)
    moving = trial["moving"]
    starts = [index for index in range(1, len(moving))
              if moving[index - 1] == 0 and moving[index] == 1]
    start = starts[0] if starts else next((index for index, value in enumerate(moving)
                                          if value == 1), None)
    if start is None or start + samples > len(filtered):
        return None
    drop = terminal_drop(filtered.tolist(), moving)
    if start + samples > drop - 10:
        return None
    curve = filtered[start:start + samples].copy()
    curve -= np.median(curve[:5])
    return curve


def build_features(curve: np.ndarray) -> tuple[list[str], np.ndarray]:
    names: list[str] = []
    values: list[float] = []
    length = len(curve)
    derivative = np.diff(curve, prepend=curve[0])

    # Position/time-localized averages let the analysis find a stable region
    # without depending on a single hand-picked endpoint window.
    for start in range(0, length, 10):
        stop = min(length, start + 10)
        names.append(f"bin_mean_{start:03d}_{stop:03d}")
        values.append(float(np.mean(curve[start:stop])))

    for width in (20, 40, 60, 100):
        for stop in (50, 100, 150, 200, 250):
            start = stop - width
            if start < 0:
                continue
            segment = curve[start:stop]
            delta = derivative[start:stop]
            prefix = f"w{width:03d}_{start:03d}_{stop:03d}"
            metrics = {
                "rise": float(np.median(segment[-max(3, width // 6):]) -
                              np.median(segment[:max(3, width // 6)])),
                "slope": slope(segment),
                "std": float(np.std(segment)),
                "range": float(np.ptp(segment)),
                "drms": float(np.sqrt(np.mean(delta ** 2))),
                "dmeanabs": float(np.mean(np.abs(delta))),
                "dmax": float(np.max(delta)),
                "dmin": float(np.min(delta)),
            }
            for metric, value in metrics.items():
                names.append(f"{prefix}_{metric}")
                values.append(value)

    for lag in (5, 10, 20, 40, 60):
        change = curve[lag:] - curve[:-lag]
        names.extend((f"all_maxrise_lag{lag}", f"all_minrise_lag{lag}",
                      f"all_rmschange_lag{lag}"))
        values.extend((float(np.max(change)), float(np.min(change)),
                       float(np.sqrt(np.mean(change ** 2)))))

    for start, stop in ((0, 100), (100, 200), (150, 250)):
        segment = curve[start:stop]
        x = np.arange(len(segment), dtype=float)
        coefficients = np.polyfit(x, segment, 1)
        detrended = segment - np.polyval(coefficients, x)
        spectrum = np.abs(np.fft.rfft(detrended)) ** 2 / len(segment)
        frequencies = np.fft.rfftfreq(len(segment), d=1 / 200.0)
        band_values = {}
        for low, high in ((2, 5), (5, 10), (10, 20), (20, 40), (40, 80)):
            mask = (frequencies >= low) & (frequencies < high)
            power = float(np.mean(spectrum[mask])) if np.any(mask) else 0.0
            band_values[f"band_{low:02d}_{high:02d}hz"] = np.log10(power + 1e-12)
        low_power = 10 ** band_values["band_02_05hz"] + 10 ** band_values["band_05_10hz"]
        high_power = 10 ** band_values["band_20_40hz"] + 10 ** band_values["band_40_80hz"]
        band_values["low_high_logratio"] = np.log10((low_power + 1e-12) /
                                                     (high_power + 1e-12))
        for metric, value in band_values.items():
            names.append(f"fft_{start:03d}_{stop:03d}_{metric}")
            values.append(float(value))
    return names, np.asarray(values, dtype=float)


def oriented_auc(values: np.ndarray, labels: np.ndarray) -> tuple[float, int]:
    score = auc(values.tolist(), labels.tolist())
    return (score, 1) if score >= 0.5 else (1 - score, -1)


def best_cut(values: np.ndarray, labels: np.ndarray) -> tuple[float, int]:
    score, direction = oriented_auc(values, labels)
    oriented = values * direction
    candidates = np.unique(oriented)
    cuts = np.r_[candidates[0] - 1e-9,
                 (candidates[:-1] + candidates[1:]) / 2,
                 candidates[-1] + 1e-9]
    best_score, best_cutoff = -1.0, 0.0
    for cutoff in cuts:
        predicted = oriented >= cutoff
        recall = np.mean(predicted[labels == 1])
        specificity = np.mean(~predicted[labels == 0])
        balanced = (recall + specificity) / 2
        if balanced > best_score:
            best_score, best_cutoff = balanced, cutoff
    return best_cutoff, direction


def stratified_folds(labels: np.ndarray, rng: random.Random, fold_count: int = 5) -> list[np.ndarray]:
    by_label = {label: np.flatnonzero(labels == label).tolist() for label in (0, 1)}
    for indices in by_label.values():
        rng.shuffle(indices)
    folds = []
    for fold in range(fold_count):
        folds.append(np.asarray(by_label[0][fold::fold_count] +
                                by_label[1][fold::fold_count], dtype=int))
    return folds


def repeated_nested_univariate(features: np.ndarray, labels: np.ndarray,
                               repeats: int = 200) -> dict:
    rng = random.Random(260826)
    accuracies, balanced_scores = [], []
    selections = np.zeros(features.shape[1], dtype=int)
    all_indices = np.arange(len(labels))
    for _ in range(repeats):
        predicted = np.zeros(len(labels), dtype=bool)
        for test in stratified_folds(labels, rng):
            train = np.setdiff1d(all_indices, test)
            train_scores = [oriented_auc(features[train, index], labels[train])[0]
                            for index in range(features.shape[1])]
            selected = int(np.argmax(train_scores))
            selections[selected] += 1
            cutoff, direction = best_cut(features[train, selected], labels[train])
            predicted[test] = features[test, selected] * direction >= cutoff
        accuracies.append(float(np.mean(predicted == labels)))
        recall = float(np.mean(predicted[labels == 1]))
        specificity = float(np.mean(~predicted[labels == 0]))
        balanced_scores.append((recall + specificity) / 2)
    return {
        "mean_accuracy": statistics.mean(accuracies),
        "accuracy_q05_q95": [float(np.quantile(accuracies, 0.05)),
                              float(np.quantile(accuracies, 0.95))],
        "mean_balanced_accuracy": statistics.mean(balanced_scores),
        "selection_counts": selections.tolist(),
    }


def repeated_ridge_waveform(curves: np.ndarray, labels: np.ndarray,
                            repeats: int = 200) -> dict:
    rng = random.Random(826260)
    all_indices = np.arange(len(labels))
    accuracies, aucs = [], []
    # Average every five samples: 50 causal shape inputs over 1.25 seconds.
    x = curves.reshape(len(curves), 50, 5).mean(axis=2)
    y = labels * 2 - 1
    for _ in range(repeats):
        scores = np.zeros(len(labels), dtype=float)
        for test in stratified_folds(labels, rng):
            train = np.setdiff1d(all_indices, test)
            mean = x[train].mean(axis=0)
            std = x[train].std(axis=0)
            std[std < 1e-6] = 1.0
            train_x = (x[train] - mean) / std
            test_x = (x[test] - mean) / std
            design = np.c_[np.ones(len(train)), train_x]
            penalty = np.eye(design.shape[1]) * 10.0
            penalty[0, 0] = 0.0
            weights = np.linalg.solve(design.T @ design + penalty,
                                      design.T @ y[train])
            scores[test] = np.c_[np.ones(len(test)), test_x] @ weights
        accuracies.append(float(np.mean((scores >= 0) == labels)))
        aucs.append(float(auc(scores.tolist(), labels.tolist())))
    return {
        "mean_accuracy": statistics.mean(accuracies),
        "accuracy_q05_q95": [float(np.quantile(accuracies, 0.05)),
                              float(np.quantile(accuracies, 0.95))],
        "mean_auc": statistics.mean(aucs),
        "auc_q05_q95": [float(np.quantile(aucs, 0.05)),
                         float(np.quantile(aucs, 0.95))],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clear", type=Path, required=True)
    parser.add_argument("--obstacle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    trials = load_trials(args.clear, 0) + load_trials(args.obstacle, 1)
    usable = [(trial, fixed_curve(trial)) for trial in trials]
    usable = [(trial, curve) for trial, curve in usable if curve is not None]
    labels = np.asarray([trial["label"] for trial, _ in usable], dtype=int)
    curves = np.stack([curve for _, curve in usable])
    names, first = build_features(curves[0])
    features = np.stack([build_features(curve)[1] for curve in curves])

    rankings = []
    for index, name in enumerate(names):
        score, direction = oriented_auc(features[:, index], labels)
        rankings.append({
            "name": name, "auc": score,
            "obstacle_direction": "higher" if direction == 1 else "lower",
            "clear_median": float(np.median(features[labels == 0, index])),
            "obstacle_median": float(np.median(features[labels == 1, index])),
        })
    rankings.sort(key=lambda item: item["auc"], reverse=True)

    nested = repeated_nested_univariate(features, labels)
    selection_counts = nested.pop("selection_counts")
    selected_ranking = sorted(
        ({"name": names[index], "count": count}
         for index, count in enumerate(selection_counts) if count),
        key=lambda item: item["count"], reverse=True,
    )[:10]
    nested["most_selected_features"] = selected_ranking

    # Time-resolved AUC is exploratory and intentionally reported with the
    # number of scanned positions to make the multiple-comparison risk clear.
    point_auc = []
    derivative = np.diff(curves, axis=1, prepend=curves[:, :1])
    for index in range(curves.shape[1]):
        value_auc, value_direction = oriented_auc(curves[:, index], labels)
        derivative_auc, derivative_direction = oriented_auc(derivative[:, index], labels)
        point_auc.append({
            "sample": index, "seconds_before_window_end": (250 - index) / 200,
            "pressure_auc": value_auc,
            "pressure_direction": "higher" if value_direction == 1 else "lower",
            "derivative_auc": derivative_auc,
            "derivative_direction": "higher" if derivative_direction == 1 else "lower",
        })

    start_usable = [(trial, motion_start_curve(trial)) for trial in trials]
    start_usable = [(trial, curve) for trial, curve in start_usable if curve is not None]
    start_labels = np.asarray([trial["label"] for trial, _ in start_usable], dtype=int)
    start_curves = np.stack([curve for _, curve in start_usable])
    start_names, _ = build_features(start_curves[0])
    start_features = np.stack([build_features(curve)[1] for curve in start_curves])
    start_rankings = []
    for index, name in enumerate(start_names):
        score, direction = oriented_auc(start_features[:, index], start_labels)
        start_rankings.append({
            "name": name, "auc": score,
            "obstacle_direction": "higher" if direction == 1 else "lower",
            "clear_median": float(np.median(start_features[start_labels == 0, index])),
            "obstacle_median": float(np.median(start_features[start_labels == 1, index])),
        })
    start_rankings.sort(key=lambda item: item["auc"], reverse=True)
    start_nested = repeated_nested_univariate(start_features, start_labels)
    start_selection_counts = start_nested.pop("selection_counts")
    start_nested["most_selected_features"] = sorted(
        ({"name": start_names[index], "count": count}
         for index, count in enumerate(start_selection_counts) if count),
        key=lambda item: item["count"], reverse=True,
    )[:10]

    result = {
        "trial_count": {"clear": int(np.sum(labels == 0)),
                        "obstacle": int(np.sum(labels == 1))},
        "analysis_window": "1.25 s ending 50 ms before terminal pressure drop",
        "candidate_feature_count": len(names),
        "top_exploratory_features_same_data": rankings[:20],
        "best_pointwise_pressure_same_data": max(point_auc, key=lambda item: item["pressure_auc"]),
        "best_pointwise_derivative_same_data": max(point_auc, key=lambda item: item["derivative_auc"]),
        "nested_repeated_5fold_single_feature": nested,
        "repeated_5fold_ridge_whole_waveform": repeated_ridge_waveform(curves, labels),
        "motion_start_aligned": {
            "usable_trials": {"clear": int(np.sum(start_labels == 0)),
                              "obstacle": int(np.sum(start_labels == 1))},
            "top_exploratory_features_same_data": start_rankings[:20],
            "nested_repeated_5fold_single_feature": start_nested,
            "repeated_5fold_ridge_whole_waveform": repeated_ridge_waveform(
                start_curves, start_labels),
        },
        "cautions": [
            "Both classes contain only one acquisition batch, so trial-level cross-validation is optimistic.",
            "Same-data feature ranking scans many correlated candidates and is exploratory only.",
            "Curves are baseline-centered, so reported shape results do not rely on absolute sensor zero offset.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
