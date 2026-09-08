#!/usr/bin/env python3
"""Generate a raw-pressure comparison SVG aligned by slider distance."""

from __future__ import annotations

import argparse
import bisect
import csv
from pathlib import Path
import statistics


def load_trials(path: Path) -> list[dict]:
    groups: dict[str, list[tuple[float, float]]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("phase") and row["phase"] != "measure":
                continue
            try:
                if not int(float(row.get("valid_mask", "1") or 0)) & 1:
                    continue
                if int(float(row.get("moving", "1") or 0)) != 1:
                    continue
                trial_id = row.get("aggregate_trial_id") or row.get("trial_id") or path.stem
                groups.setdefault(trial_id, []).append(
                    (float(row["position_mm"]), float(row["pressure_pa_1"])))
            except (KeyError, TypeError, ValueError):
                continue

    trials = []
    for trial_id, samples in groups.items():
        by_position: dict[float, list[float]] = {}
        for position, pressure in samples:
            by_position.setdefault(position, []).append(pressure)
        positions = sorted(by_position)
        if len(positions) < 20:
            continue
        pressures = [statistics.mean(by_position[position]) for position in positions]
        trials.append({"id": trial_id, "positions": positions, "pressures": pressures})
    return trials


def interpolate(trial: dict, position: float) -> float:
    positions, pressures = trial["positions"], trial["pressures"]
    index = bisect.bisect_left(positions, position)
    if index <= 0:
        return pressures[0]
    if index >= len(positions):
        return pressures[-1]
    left_x, right_x = positions[index - 1], positions[index]
    ratio = (position - left_x) / max(right_x - left_x, 1e-9)
    return pressures[index - 1] + ratio * (pressures[index] - pressures[index - 1])


def write_svg(clear: list[dict], obstacle: list[dict], output: Path,
              start_mm: float, end_mm: float, step_mm: float,
              all_trials: bool = False) -> Path:
    if not clear or not obstacle:
        raise ValueError("有障碍和无障碍数据都必须包含有效运动段")
    count = int(round((end_mm - start_mm) / step_mm))
    grid = [start_mm + (end_mm - start_mm) * index / count
            for index in range(count + 1)]
    trial_curves = {
        0: [[interpolate(trial, position) for position in grid] for trial in clear],
        1: [[interpolate(trial, position) for position in grid] for trial in obstacle],
    }
    medians = {label: [statistics.median(curve[index] for curve in curves)
                       for index in range(len(grid))]
               for label, curves in trial_curves.items()}

    width, height = 1400, 720
    left, right, top, bottom = 90, 30, 65, 75
    plot_w, plot_h = width - left - right, height - top - bottom
    values = ([value for curves in trial_curves.values()
               for curve in curves for value in curve]
              if all_trials else medians[0] + medians[1])
    low, high = min(values), max(values)
    padding = max((high - low) * 0.1, 0.01)
    low, high = low - padding, high + padding
    span = high - low

    def point(position: float, value: float) -> tuple[float, float]:
        x = left + (position - start_mm) / (end_mm - start_mm) * plot_w
        y = top + (high - value) / span * plot_h
        return x, y

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{left}" y="31" font-family="sans-serif" font-size="22" font-weight="bold">{"逐次压力数据" if all_trials else "原始压力数据"} · 按滑台距离对齐</text>',
        f'<text x="{left}" y="52" font-family="sans-serif" font-size="13" fill="#64748b">CH1 · 不做二次滤波 · 按距离线性对齐 · 无障碍 {len(clear)} 次 · 有障碍 {len(obstacle)} 次 · {"每次实验直接绘制" if all_trials else "组内中位数"}</text>',
        f'<rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" fill="#f8fafc" stroke="#94a3b8"/>',
    ]
    for tick in range(6):
        x = left + plot_w * tick / 5
        position = start_mm + (end_mm - start_mm) * tick / 5
        y = top + plot_h * tick / 5
        pressure = high - span * tick / 5
        lines.extend([
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_h}" stroke="#e2e8f0"/>',
            f'<text x="{x:.1f}" y="{top + plot_h + 24}" text-anchor="middle" font-family="sans-serif" font-size="12">{position:.0f}</text>',
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="#e2e8f0"/>',
            f'<text x="{left - 9}" y="{y + 4:.1f}" text-anchor="end" font-family="sans-serif" font-size="12">{pressure:.3f}</text>',
        ])
    for label, color in ((0, "#2563eb"), (1, "#f97316")):
        curves = trial_curves[label] if all_trials else [medians[label]]
        for curve in curves:
            points = " ".join(
                f"{x:.1f},{y:.1f}" for x, y in
                (point(position, value) for position, value in zip(grid, curve)))
            lines.append(
                f'<polyline points="{points}" fill="none" stroke="{color}" '
                f'stroke-width="{0.9 if all_trials else 2.2}" '
                f'opacity="{0.30 if all_trials else 1.0}"/>')
    lines.extend([
        f'<text x="{left + plot_w / 2}" y="{height - 24}" text-anchor="middle" font-family="sans-serif" font-size="14">滑台位置 (mm)</text>',
        f'<text x="24" y="{top + plot_h / 2}" transform="rotate(-90 24 {top + plot_h / 2})" text-anchor="middle" font-family="sans-serif" font-size="14">原始压力 (Pa)</text>',
        f'<line x1="{left + 20}" y1="{top + 20}" x2="{left + 70}" y2="{top + 20}" stroke="#2563eb" stroke-width="2.2"/><text x="{left + 80}" y="{top + 25}" font-family="sans-serif" font-size="13">无障碍</text>',
        f'<line x1="{left + 180}" y1="{top + 20}" x2="{left + 230}" y2="{top + 20}" stroke="#f97316" stroke-width="2.2"/><text x="{left + 240}" y="{top + 25}" font-family="sans-serif" font-size="13">有障碍</text>',
        '</svg>',
    ])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def write_split_svgs(clear: list[dict], obstacle: list[dict], output_dir: Path,
                     start_mm: float, end_mm: float, step_mm: float) -> list[Path]:
    """Write one all-trial SVG per condition using the same x/y scales."""
    if not clear or not obstacle:
        raise ValueError("有障碍和无障碍数据都必须包含有效运动段")
    count = int(round((end_mm - start_mm) / step_mm))
    grid = [start_mm + (end_mm - start_mm) * index / count
            for index in range(count + 1)]
    groups = [
        ("无障碍", "#2563eb", clear, "clear_trials_by_distance.svg"),
        ("有障碍", "#f97316", obstacle, "obstacle_trials_by_distance.svg"),
    ]
    curves_by_name = {
        name: [[interpolate(trial, position) for position in grid]
               for trial in trials]
        for name, _color, trials, _filename in groups
    }
    values = [value for curves in curves_by_name.values()
              for curve in curves for value in curve]
    low, high = min(values), max(values)
    padding = max((high - low) * 0.1, 0.01)
    low, high = low - padding, high + padding
    span = high - low

    width, height = 1400, 720
    left, right, top, bottom = 90, 30, 65, 75
    plot_w, plot_h = width - left - right, height - top - bottom

    def point(position: float, value: float) -> tuple[float, float]:
        x = left + (position - start_mm) / (end_mm - start_mm) * plot_w
        y = top + (high - value) / span * plot_h
        return x, y

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for name, color, trials, filename in groups:
        lines = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            '<rect width="100%" height="100%" fill="#ffffff"/>',
            f'<text x="{left}" y="31" font-family="sans-serif" font-size="22" font-weight="bold">{name} · 逐次压力数据</text>',
            f'<text x="{left}" y="52" font-family="sans-serif" font-size="13" fill="#64748b">CH1 · CSV 原值 · 不做二次滤波 · 按滑台距离对齐 · {len(trials)} 次实验直接绘制</text>',
            f'<rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" fill="#f8fafc" stroke="#94a3b8"/>',
        ]
        for tick in range(6):
            x = left + plot_w * tick / 5
            position = start_mm + (end_mm - start_mm) * tick / 5
            y = top + plot_h * tick / 5
            pressure = high - span * tick / 5
            lines.extend([
                f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_h}" stroke="#e2e8f0"/>',
                f'<text x="{x:.1f}" y="{top + plot_h + 24}" text-anchor="middle" font-family="sans-serif" font-size="12">{position:.0f}</text>',
                f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="#e2e8f0"/>',
                f'<text x="{left - 9}" y="{y + 4:.1f}" text-anchor="end" font-family="sans-serif" font-size="12">{pressure:.3f}</text>',
            ])
        for curve in curves_by_name[name]:
            points = " ".join(
                f"{x:.1f},{y:.1f}" for x, y in
                (point(position, value) for position, value in zip(grid, curve)))
            lines.append(
                f'<polyline points="{points}" fill="none" stroke="{color}" '
                'stroke-width="0.9" opacity="0.38"/>')
        lines.extend([
            f'<text x="{left + plot_w / 2}" y="{height - 24}" text-anchor="middle" font-family="sans-serif" font-size="14">滑台位置 (mm)</text>',
            f'<text x="24" y="{top + plot_h / 2}" transform="rotate(-90 24 {top + plot_h / 2})" text-anchor="middle" font-family="sans-serif" font-size="14">原始压力 (Pa)</text>',
            f'<line x1="{left + 20}" y1="{top + 20}" x2="{left + 70}" y2="{top + 20}" stroke="{color}" stroke-width="2.2"/><text x="{left + 80}" y="{top + 25}" font-family="sans-serif" font-size="13">{name}</text>',
            '</svg>',
        ])
        output = output_dir / filename
        output.write_text("\n".join(lines), encoding="utf-8")
        outputs.append(output)
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clear", type=Path, required=True)
    parser.add_argument("--obstacle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-mm", type=float, default=10.0)
    parser.add_argument("--end-mm", type=float, default=1900.0)
    parser.add_argument("--step-mm", type=float, default=5.0)
    parser.add_argument("--all-trials", action="store_true",
                        help="直接叠加每次实验，不计算组内中位数")
    parser.add_argument("--split-dir", type=Path,
                        help="分别输出无障碍和有障碍逐次曲线，并共用相同坐标范围")
    args = parser.parse_args()
    if args.end_mm <= args.start_mm or args.step_mm <= 0:
        parser.error("距离范围或步长无效")
    clear = load_trials(args.clear)
    obstacle = load_trials(args.obstacle)
    if args.split_dir:
        outputs = write_split_svgs(clear, obstacle, args.split_dir,
                                   args.start_mm, args.end_mm, args.step_mm)
        for output in outputs:
            print(f"生成完成: {output}")
        return
    output = write_svg(clear, obstacle, args.output,
                       args.start_mm, args.end_mm, args.step_mm,
                       args.all_trials)
    print(f"生成完成: {output} (无障碍{len(clear)}次，有障碍{len(obstacle)}次)")


if __name__ == "__main__":
    main()
