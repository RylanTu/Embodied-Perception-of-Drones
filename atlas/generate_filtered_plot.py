#!/usr/bin/env python3
"""Generate an overlaid, filtered SVG from one CSV or a directory of CSV files."""

from __future__ import annotations

import argparse
import bisect
import csv
from html import escape
from pathlib import Path
import statistics

def _kalman_one_way(values: list[float], process_noise: float,
                    measurement_noise: float) -> list[float]:
    """Scalar random-walk Kalman filter for one pressure trace."""
    estimate = values[0]
    covariance = measurement_noise
    result = [estimate]
    for measurement in values[1:]:
        predicted_covariance = covariance + process_noise
        gain = predicted_covariance / (predicted_covariance + measurement_noise)
        estimate += gain * (measurement - estimate)
        covariance = (1.0 - gain) * predicted_covariance
        result.append(estimate)
    return result


def kalman_zero_phase(values: list[float], process_noise: float,
                      measurement_noise: float) -> list[float]:
    """Average forward/backward Kalman estimates to avoid visible time lag."""
    if len(values) < 2:
        return list(values)
    forward = _kalman_one_way(values, process_noise, measurement_noise)
    backward = list(reversed(_kalman_one_way(
        list(reversed(values)), process_noise, measurement_noise)))
    return [(left + right) * 0.5 for left, right in zip(forward, backward)]


def load_trials(paths: list[Path], channel: int, process_noise: float,
                measurement_noise: float, kalman_mode: str) -> list[dict]:
    column = f"pressure_pa_{channel}"
    trials: list[dict] = []
    for path in paths:
        groups: dict[str, list[dict]] = {}
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                if row.get("phase") and row["phase"] != "measure":
                    continue
                groups.setdefault(row.get("trial_id") or path.stem, []).append(row)
        for trial_id, rows in groups.items():
            try:
                start_ms = float(rows[0].get("esp_uptime_ms", 0) or 0)
                time_s, raw = [], []
                for index, row in enumerate(rows):
                    valid_mask = int(float(row.get("valid_mask", 31) or 0))
                    if not valid_mask & (1 << (channel - 1)):
                        continue
                    time_s.append((float(row.get("esp_uptime_ms", index)) - start_ms) / 1000.0)
                    raw.append(float(row[column]))
                if len(raw) < 2:
                    continue
                if kalman_mode == "one-way":
                    filtered = _kalman_one_way(
                        raw, process_noise, measurement_noise)
                else:
                    filtered = kalman_zero_phase(
                        raw, process_noise, measurement_noise)
                stride = max(1, len(raw) // 1000)
                trials.append({
                    "name": f"{path.stem}/{trial_id}",
                    "label": int(float(rows[0].get("label", 0) or 0)),
                    "time": time_s[::stride],
                    "raw": raw[::stride],
                    "filtered": filtered[::stride],
                })
            except (KeyError, TypeError, ValueError):
                continue
    return trials


def write_svg(trials: list[dict], output: Path, channel: int,
              process_noise: float, measurement_noise: float,
              show_raw: bool, kalman_mode: str) -> Path:
    if not trials:
        raise ValueError(f"没有找到有效的 pressure_pa_{channel} 数据")
    width, height = 1500, 760
    left, right, top, bottom = 95, 35, 70, 80
    plot_w, plot_h = width - left - right, height - top - bottom
    values = [value for trial in trials for value in trial["filtered"]]
    low, high = min(values), max(values)
    padding = max((high - low) * 0.08, 0.01)
    low -= padding
    high += padding
    span = high - low
    max_time = max(max(trial["time"]) for trial in trials) or 1.0
    no_count = sum(trial["label"] == 0 for trial in trials)
    yes_count = sum(trial["label"] == 1 for trial in trials)

    def point(time_s: float, value: float) -> tuple[float, float]:
        return (left + time_s / max_time * plot_w,
                top + (high - value) / span * plot_h)

    def interpolate(trial: dict, target: float) -> float:
        times, values = trial["time"], trial["filtered"]
        index = bisect.bisect_left(times, target)
        if index <= 0:
            return values[0]
        if index >= len(times):
            return values[-1]
        left_t, right_t = times[index - 1], times[index]
        ratio = (target - left_t) / max(right_t - left_t, 1e-9)
        return values[index - 1] + ratio * (values[index] - values[index - 1])

    mode_label = "单向因果卡尔曼" if kalman_mode == "one-way" else "双向零相位卡尔曼"
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{left}" y="30" font-family="sans-serif" font-size="22" font-weight="bold">{escape(output.stem)} · CH{channel}</text>',
        f'<text x="{left}" y="53" font-family="sans-serif" font-size="13" fill="#64748b">{len(trials)} trials · 无障碍 {no_count} · 有障碍 {yes_count} · {mode_label} Q={process_noise:g} Pa² · R={measurement_noise:g} Pa²</text>',
        f'<rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" fill="#f8fafc" stroke="#94a3b8"/>',
    ]
    for tick in range(6):
        x = left + plot_w * tick / 5
        t = max_time * tick / 5
        y = top + plot_h * tick / 5
        value = high - span * tick / 5
        lines.extend([
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_h}" stroke="#e2e8f0"/>',
            f'<text x="{x:.1f}" y="{top + plot_h + 24}" text-anchor="middle" font-family="sans-serif" font-size="12" fill="#475569">{t:.2f}</text>',
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="#e2e8f0"/>',
            f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" font-family="sans-serif" font-size="12" fill="#475569">{value:.3f}</text>',
        ])
    for trial in trials:
        color = "#dc2626" if trial["label"] else "#2563eb"
        dash = ' stroke-dasharray="7 4"' if trial["label"] else ""
        if show_raw:
            raw_points = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                                  (point(t, v) for t, v in zip(trial["time"], trial["raw"])))
            lines.append(f'<polyline points="{raw_points}" fill="none" stroke="{color}" stroke-width="0.7" opacity="0.035"/>')
        filtered_points = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                                   (point(t, v) for t, v in zip(trial["time"], trial["filtered"])))
        lines.append(f'<polyline points="{filtered_points}" fill="none" stroke="{color}" stroke-width="1.0" opacity="0.10"{dash}/>')

    # A thick pointwise median makes the common plateau visible even when
    # hundreds of individual experiments overlap.
    grid = [max_time * index / 599 for index in range(600)]
    for label, color in ((0, "#1d4ed8"), (1, "#b91c1c")):
        group = [trial for trial in trials if trial["label"] == label]
        if not group:
            continue
        median_values = [statistics.median(interpolate(trial, target)
                                           for trial in group)
                         for target in grid]
        median_points = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                                 (point(t, v) for t, v in zip(grid, median_values)))
        dash = ' stroke-dasharray="12 6"' if label else ""
        lines.append(f'<polyline points="{median_points}" fill="none" stroke="#ffffff" stroke-width="7" opacity="0.9"{dash}/>')
        lines.append(f'<polyline points="{median_points}" fill="none" stroke="{color}" stroke-width="4" opacity="0.98"{dash}/>')
    lines.extend([
        f'<text x="{left + plot_w / 2}" y="{height - 24}" text-anchor="middle" font-family="sans-serif" font-size="14">实验开始后的时间 (s)</text>',
        f'<text x="24" y="{top + plot_h / 2}" transform="rotate(-90 24 {top + plot_h / 2})" text-anchor="middle" font-family="sans-serif" font-size="14">压力 (Pa)</text>',
        f'<line x1="{left + 15}" y1="{top + 18}" x2="{left + 65}" y2="{top + 18}" stroke="#2563eb" stroke-width="3"/><text x="{left + 75}" y="{top + 23}" font-family="sans-serif" font-size="13">无障碍</text>',
        f'<line x1="{left + 165}" y1="{top + 18}" x2="{left + 215}" y2="{top + 18}" stroke="#dc2626" stroke-width="3" stroke-dasharray="7 4"/><text x="{left + 225}" y="{top + 23}" font-family="sans-serif" font-size="13">有障碍</text>',
        f'<text x="{left + 330}" y="{top + 23}" font-family="sans-serif" font-size="13" fill="#475569">粗线=组内中位数，细线=单次实验</text>',
        '</svg>',
    ])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="一个CSV文件或包含CSV的目录")
    parser.add_argument("--output", type=Path, help="输出SVG路径")
    parser.add_argument("--channel", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--kalman-q", type=float, default=0.03,
                        help="过程噪声方差Pa²；越大越跟随原始变化")
    parser.add_argument("--kalman-r", type=float, default=0.02,
                        help="测量噪声方差Pa²；越大越平滑")
    parser.add_argument("--kalman-mode", choices=("one-way", "zero-phase"),
                        default="zero-phase",
                        help="one-way为实时单向滤波；zero-phase为离线双向滤波")
    parser.add_argument("--show-raw", action="store_true", help="同时用浅色显示原始曲线")
    args = parser.parse_args()
    if args.kalman_q <= 0 or args.kalman_r <= 0:
        parser.error("--kalman-q和--kalman-r必须大于0")
    if args.input.is_file():
        paths = [args.input]
        default_output = args.input.with_name(args.input.stem + "_filtered.svg")
    elif args.input.is_dir():
        paths = sorted(args.input.rglob("*.csv"))
        default_output = args.input / "filtered_comparison.svg"
    else:
        parser.error("输入路径不存在")
    trials = load_trials(paths, args.channel, args.kalman_q, args.kalman_r,
                         args.kalman_mode)
    output = write_svg(trials, args.output or default_output, args.channel,
                       args.kalman_q, args.kalman_r, args.show_raw,
                       args.kalman_mode)
    print(f"生成完成: {output} ({len(trials)}次实验)")


if __name__ == "__main__":
    main()
