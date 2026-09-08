#!/usr/bin/env python3
"""Trial-level discriminability analysis for paired pressure CSV datasets."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import statistics


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def median(values: list[float]) -> float:
    return statistics.median(values)


def quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    position = fraction * (len(ordered) - 1)
    left = int(position)
    right = min(left + 1, len(ordered) - 1)
    ratio = position - left
    return ordered[left] * (1 - ratio) + ordered[right] * ratio


def kalman(values: list[float], q: float = 0.03, r: float = 0.02) -> list[float]:
    estimate = values[0]
    covariance = r
    output = [estimate]
    for measurement in values[1:]:
        predicted_covariance = covariance + q
        gain = predicted_covariance / (predicted_covariance + r)
        estimate += gain * (measurement - estimate)
        covariance = (1 - gain) * predicted_covariance
        output.append(estimate)
    return output


def load_trials(path: Path, assigned_label: int) -> list[dict]:
    groups: dict[str, dict] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_index, row in enumerate(csv.DictReader(stream)):
            if row.get("phase") and row["phase"] != "measure":
                continue
            source = row.get("source_run", "")
            trial_id = (row.get("aggregate_trial_id") or
                        (f"{source}/{row.get('trial_id', '')}" if source else row.get("trial_id")) or
                        path.stem)
            group = groups.setdefault(trial_id, {
                "id": trial_id, "source": source or path.stem,
                "pressure": [], "time_ms": [], "moving": [],
                "stored_labels": set(), "row_index": [],
            })
            try:
                valid = int(float(row.get("valid_mask", "1") or 0)) & 1
                if not valid:
                    continue
                group["pressure"].append(float(row["pressure_pa_1"]))
                group["time_ms"].append(float(row.get("esp_uptime_ms", row_index) or row_index))
                group["moving"].append(int(float(row.get("moving", "1") or 0)))
                group["stored_labels"].add(int(float(row.get("label", assigned_label) or 0)))
                group["row_index"].append(row_index)
            except (KeyError, TypeError, ValueError):
                continue
    trials = []
    for group in groups.values():
        if len(group["pressure"]) < 220:
            continue
        group["label"] = assigned_label
        trials.append(group)
    return trials


def terminal_drop(values: list[float], moving: list[int]) -> int:
    length = len(values)
    smooth_width = max(3, min(11, length // 50 * 2 + 1))
    half = smooth_width // 2
    smooth = []
    for index in range(length):
        lo, hi = max(0, index - half), min(length, index + half + 1)
        smooth.append(sum(values[lo:hi]) / (hi - lo))
    derivative = [0.0] + [smooth[i] - smooth[i - 1] for i in range(1, length)]
    transitions = [i for i in range(1, length) if moving[i - 1] == 1 and moving[i] == 0]
    if transitions:
        stop_at = transitions[-1]
        start = max(2, int(length * 0.45), stop_at - 70)
        stop = min(length - 2, stop_at + 5)
    else:
        start, stop = max(2, int(length * 0.65)), max(3, int(length * 0.95))
    return min(range(start, stop), key=lambda i: derivative[i]) if stop > start else length - 1


def linear_slope(values: list[float], sample_rate: float) -> float:
    count = len(values)
    center = (count - 1) / 2
    denominator = sum((index - center) ** 2 for index in range(count))
    numerator = sum((index - center) * value for index, value in enumerate(values))
    return numerator / denominator * sample_rate if denominator else 0.0


def features_for(trial: dict, filtered: bool) -> dict | None:
    raw = trial["pressure"]
    values = kalman(raw) if filtered else raw
    drop = terminal_drop(values, trial["moving"])
    end = drop - 10
    if end < 200:
        return None
    segment = values[end - 200:end]
    deltas = [right - left for left, right in zip(trial["time_ms"], trial["time_ms"][1:])
              if 1 <= right - left <= 30]
    sample_rate = 1000 / median(deltas) if deltas else 200.0
    last_300 = segment[-60:]
    previous_300 = segment[-120:-60]
    return {
        "rise_300ms": median(last_300[-10:]) - median(last_300[:10]),
        "slope_300ms": linear_slope(last_300, sample_rate),
        "slope_change": linear_slope(last_300, sample_rate) - linear_slope(previous_300, sample_rate),
        "peak_rise": max(segment[-80:]) - median(segment[-120:-80]),
        "range_1s": max(segment) - min(segment),
        "drop": drop,
        "sample_rate": sample_rate,
        "curve": segment,
    }


def auc(values: list[float], labels: list[int]) -> float:
    positives = [v for v, label in zip(values, labels) if label == 1]
    negatives = [v for v, label in zip(values, labels) if label == 0]
    wins = sum(1.0 if positive > negative else 0.5 if positive == negative else 0.0
               for positive in positives for negative in negatives)
    return wins / (len(positives) * len(negatives))


def best_threshold(values: list[float], labels: list[int]) -> dict:
    candidates = sorted(set(values))
    candidates = ([candidates[0] - 1e-9] +
                  [(a + b) / 2 for a, b in zip(candidates, candidates[1:])] +
                  [candidates[-1] + 1e-9])
    best = None
    for direction in (1, -1):
        for threshold in candidates:
            predicted = [direction * value >= direction * threshold for value in values]
            recall = sum(p and y == 1 for p, y in zip(predicted, labels)) / sum(labels)
            specificity = sum((not p) and y == 0 for p, y in zip(predicted, labels)) / (len(labels) - sum(labels))
            score = (recall + specificity) / 2
            if best is None or score > best["balanced_accuracy"]:
                best = {"threshold": threshold, "direction": ">=" if direction == 1 else "<=",
                        "recall": recall, "specificity": specificity,
                        "balanced_accuracy": score}
    return best


def effect_size(values: list[float], labels: list[int]) -> float:
    yes = [v for v, y in zip(values, labels) if y]
    no = [v for v, y in zip(values, labels) if not y]
    pooled = math.sqrt(((len(yes) - 1) * statistics.variance(yes) +
                        (len(no) - 1) * statistics.variance(no)) /
                       (len(yes) + len(no) - 2))
    return (statistics.mean(yes) - statistics.mean(no)) / pooled if pooled else 0.0


def bootstrap_auc(values: list[float], labels: list[int], repeats: int = 2000) -> list[float]:
    rng = random.Random(20260826)
    yes = [v for v, y in zip(values, labels) if y]
    no = [v for v, y in zip(values, labels) if not y]
    estimates = []
    for _ in range(repeats):
        sampled_yes = [rng.choice(yes) for _ in yes]
        sampled_no = [rng.choice(no) for _ in no]
        estimates.append(auc(sampled_yes + sampled_no, [1] * len(yes) + [0] * len(no)))
    return [quantile(estimates, 0.025), quantile(estimates, 0.975)]


def write_svg(records: list[dict], output: Path) -> None:
    width, height = 1400, 720
    left, right, top, bottom = 90, 30, 65, 75
    plot_w, plot_h = width - left - right, height - top - bottom
    curves = [record["filtered_features"]["curve"] for record in records]
    groups = {label: [curve for curve, record in zip(curves, records) if record["label"] == label]
              for label in (0, 1)}
    median_curves = {label: [median([curve[index] for curve in group])
                             for index in range(200)] for label, group in groups.items()}
    values = [value for curve in median_curves.values() for value in curve]
    low, high = min(values), max(values)
    pad = max((high - low) * 0.12, 0.01)
    low, high = low - pad, high + pad
    span = high - low

    def point(index: int, value: float) -> tuple[float, float]:
        return (left + index / 199 * plot_w, top + (high - value) / span * plot_h)

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{left}" y="31" font-family="sans-serif" font-size="22" font-weight="bold">终点下降前 1 秒 · 单向卡尔曼组内中位数</text>',
        f'<text x="{left}" y="52" font-family="sans-serif" font-size="13" fill="#64748b">CH1 · Q=0.03 Pa² · R=0.02 Pa² · 末端保留50 ms保护区</text>',
        f'<rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" fill="#f8fafc" stroke="#94a3b8"/>',
    ]
    for tick in range(6):
        x = left + tick / 5 * plot_w
        y = top + tick / 5 * plot_h
        lines.extend([
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_h}" stroke="#e2e8f0"/>',
            f'<text x="{x:.1f}" y="{top + plot_h + 24}" text-anchor="middle" font-family="sans-serif" font-size="12">{-1 + tick / 5:.1f}</text>',
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="#e2e8f0"/>',
            f'<text x="{left - 9}" y="{y + 4:.1f}" text-anchor="end" font-family="sans-serif" font-size="12">{high - tick / 5 * span:.3f}</text>',
        ])
    for label, color in ((0, "#2563eb"), (1, "#f97316")):
        curve = median_curves[label]
        points = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                          (point(index, value) for index, value in enumerate(curve)))
        lines.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.2"/>')
    lines.extend([
        f'<text x="{left + plot_w / 2}" y="{height - 24}" text-anchor="middle" font-family="sans-serif" font-size="14">相对分析窗口结束时间 (s)</text>',
        f'<text x="24" y="{top + plot_h / 2}" transform="rotate(-90 24 {top + plot_h / 2})" text-anchor="middle" font-family="sans-serif" font-size="14">压力 (Pa)</text>',
        f'<line x1="{left + 20}" y1="{top + 20}" x2="{left + 70}" y2="{top + 20}" stroke="#2563eb" stroke-width="2.2"/><text x="{left + 80}" y="{top + 25}" font-family="sans-serif" font-size="13">无障碍</text>',
        f'<line x1="{left + 180}" y1="{top + 20}" x2="{left + 230}" y2="{top + 20}" stroke="#f97316" stroke-width="2.2"/><text x="{left + 240}" y="{top + 25}" font-family="sans-serif" font-size="13">有障碍</text>',
        '</svg>',
    ])
    output.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--obstacle", type=Path, required=True)
    parser.add_argument("--clear", type=Path, required=True)
    parser.add_argument("--prop-obstacle", type=Path, required=True)
    parser.add_argument("--prop-clear", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    obstacle = load_trials(args.obstacle, 1)
    clear = load_trials(args.clear, 0)
    records = []
    for trial in clear + obstacle:
        raw_features = features_for(trial, False)
        filtered_features = features_for(trial, True)
        if raw_features and filtered_features:
            records.append({**trial, "raw_features": raw_features,
                            "filtered_features": filtered_features})

    labels = [record["label"] for record in records]
    summary = {
        "files": {
            "obstacle": str(args.obstacle), "clear": str(args.clear),
            "prop_obstacle": str(args.prop_obstacle), "prop_clear": str(args.prop_clear),
        },
        "propeller_files_identical": file_hash(args.prop_obstacle) == file_hash(args.prop_clear),
        "trials": {
            "obstacle": sum(labels), "clear": len(labels) - sum(labels),
            "obstacle_sources": len({record["source"] for record in records if record["label"]}),
            "clear_sources": len({record["source"] for record in records if not record["label"]}),
        },
        "sample_rate_hz": {
            "median": median([record["filtered_features"]["sample_rate"] for record in records]),
            "q1": quantile([record["filtered_features"]["sample_rate"] for record in records], 0.25),
            "q3": quantile([record["filtered_features"]["sample_rate"] for record in records], 0.75),
        },
        "stored_label_values": {
            "obstacle": sorted(set().union(*(record["stored_labels"] for record in records if record["label"]))),
            "clear": sorted(set().union(*(record["stored_labels"] for record in records if not record["label"]))),
        },
        "features": {},
    }
    feature_names = ["rise_300ms", "slope_300ms", "slope_change", "peak_rise", "range_1s"]
    for mode in ("raw", "filtered"):
        summary["features"][mode] = {}
        for name in feature_names:
            values = [record[f"{mode}_features"][name] for record in records]
            yes = [value for value, label in zip(values, labels) if label]
            no = [value for value, label in zip(values, labels) if not label]
            measured_auc = auc(values, labels)
            oriented_auc = max(measured_auc, 1 - measured_auc)
            summary["features"][mode][name] = {
                "clear_mean": statistics.mean(no), "obstacle_mean": statistics.mean(yes),
                "clear_median": median(no), "obstacle_median": median(yes),
                "cohen_d": effect_size(values, labels), "auc": measured_auc,
                "oriented_auc": oriented_auc,
                "auc_95ci": bootstrap_auc(values if measured_auc >= 0.5 else [-v for v in values], labels),
                "best_same_data_threshold": best_threshold(values, labels),
            }
    output_json = args.output / "new_data_discriminability.json"
    output_svg = args.output / "new_data_discriminability_one_way.svg"
    output_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_svg(records, output_svg)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"JSON: {output_json}")
    print(f"SVG: {output_svg}")


if __name__ == "__main__":
    main()
