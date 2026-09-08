"""Dependency-free SVG plots for Atlas pressure capture CSV files."""

from __future__ import annotations

import csv
from html import escape
from pathlib import Path


COLORS = ["#16a085", "#e67e22", "#2980b9", "#8e44ad", "#c0392b",
          "#2c3e50", "#d35400", "#27ae60", "#7f8c8d", "#f39c12"]
DEFAULT_FILTER = {"enabled": True, "median_window": 5, "ema_alpha": 0.35}


def _smooth_pressure(values: list[float], filter_config: dict | None = None) -> list[float]:
    """Smooth one plotted trace without changing the source samples.

    The centered median rejects isolated spikes. Running EMA in both directions
    avoids the time shift of a causal EMA, which matters when comparing ramps.
    """
    config = {**DEFAULT_FILTER, **(filter_config or {})}
    if not config["enabled"] or len(values) < 3:
        return list(values)

    window = int(config["median_window"])
    radius = window // 2
    median_values = []
    for index in range(len(values)):
        neighborhood = values[max(0, index - radius):min(len(values), index + radius + 1)]
        ordered = sorted(neighborhood)
        median_values.append(ordered[len(ordered) // 2])

    alpha = float(config["ema_alpha"])

    def ema(samples: list[float]) -> list[float]:
        result = [samples[0]]
        for sample in samples[1:]:
            result.append(alpha * sample + (1.0 - alpha) * result[-1])
        return result

    forward = ema(median_values)
    return list(reversed(ema(list(reversed(forward)))))


def _filter_description(filter_config: dict | None) -> str:
    config = {**DEFAULT_FILTER, **(filter_config or {})}
    if not config["enabled"]:
        return "plot filter: off"
    median = f"median {int(config['median_window'])}" if int(config["median_window"]) > 1 else "median off"
    return f"plot filter: {median} + zero-phase EMA {float(config['ema_alpha']):.2f}"


def _load_trials(paths: list[Path], filter_config: dict | None = None) -> list[dict]:
    trials: list[dict] = []
    for path in paths:
        groups: dict[str, list[dict]] = {}
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                if row.get("phase") and row["phase"] != "measure":
                    continue
                groups.setdefault(row.get("trial_id") or path.stem, []).append(row)
        for trial_id, rows in groups.items():
            if not rows:
                continue
            try:
                start_ms = float(rows[0].get("esp_uptime_ms", 0))
                times = [(float(row.get("esp_uptime_ms", index)) - start_ms) / 1000
                         for index, row in enumerate(rows)]
                pressure = [_smooth_pressure(
                    [float(row[f"pressure_pa_{channel}"]) for row in rows], filter_config)
                    for channel in range(1, 6)]
                stride = max(1, len(rows) // 500)
                trials.append({
                    "name": trial_id,
                    "label": int(float(rows[0].get("label", 0) or 0)),
                    "time": times[::stride],
                    "pressure": [channel[::stride] for channel in pressure],
                })
            except (KeyError, TypeError, ValueError):
                continue
    return trials


def _write_svg(trials: list[dict], output: Path, title: str,
               filter_config: dict | None = None) -> Path:
    width, height, left, plot_width, row_height = 1500, 760, 72, 1395, 136
    values = [value for trial in trials for channel in trial["pressure"] for value in channel]
    low, high = (min(values), max(values)) if values else (-1.0, 1.0)
    padding = max((high - low) * .05, .01)
    low -= padding; high += padding; span = high - low
    max_time = max((max(trial["time"], default=0) for trial in trials), default=1) or 1
    label_zero = sum(trial["label"] == 0 for trial in trials)
    label_one = sum(trial["label"] == 1 for trial in trials)
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="72" y="27" font-family="sans-serif" font-size="20" font-weight="bold">{escape(title)}</text>',
        f'<text x="72" y="48" font-family="sans-serif" font-size="13" fill="#64748b">{len(trials)} trials · label 0: {label_zero} · label 1: {label_one} · pressure {low:.3f}…{high:.3f} Pa · {_filter_description(filter_config)}</text>',
    ]
    for channel in range(5):
        top = 60 + channel * row_height
        lines.append(f'<rect x="{left}" y="{top}" width="{plot_width}" height="112" fill="#f8fafc" stroke="#cbd5e1"/>')
        lines.append(f'<text x="16" y="{top + 21}" font-family="sans-serif" font-size="14">CH{channel + 1}</text>')
        lines.append(f'<text x="{left}" y="{top + 128}" font-family="sans-serif" font-size="11" fill="#64748b">0 s</text>')
        lines.append(f'<text x="{left + plot_width - 45}" y="{top + 128}" font-family="sans-serif" font-size="11" fill="#64748b">{max_time:.2f} s</text>')
        for index, trial in enumerate(trials):
            data, times = trial["pressure"][channel], trial["time"]
            if len(data) < 2:
                continue
            points = " ".join(
                f"{left + times[i] / max_time * plot_width:.1f},{top + 107 - (value - low) / span * 102:.1f}"
                for i, value in enumerate(data)
            )
            dash = ' stroke-dasharray="6 3"' if trial["label"] else ""
            lines.append(f'<polyline points="{points}" fill="none" stroke="{COLORS[index % len(COLORS)]}" stroke-width="1.2" opacity=".72"{dash}/>' )
    lines.append('<text x="72" y="750" font-family="sans-serif" font-size="12" fill="#64748b">实线=无障碍(label 0)，虚线=有障碍(label 1)；横轴按实验开始时间对齐。</text>')
    lines.append('</svg>')
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def generate_batch_plot(csv_path: Path, filter_config: dict | None = None) -> Path:
    """Generate one image containing every trial in a single batch CSV."""
    output = csv_path.with_suffix(".svg")
    return _write_svg(_load_trials([csv_path], filter_config), output,
                      f"Batch: {csv_path.stem}", filter_config)


def generate_condition_plot(condition_dir: Path, filter_config: dict | None = None) -> Path:
    """Overlay every trial from every CSV under the same condition directory."""
    output = condition_dir / "condition_comparison.svg"
    return _write_svg(_load_trials(sorted(condition_dir.glob("*.csv")), filter_config), output,
                      f"Condition comparison: {condition_dir.name}", filter_config)


def generate_experiment_plots(csv_path: Path, filter_config: dict | None = None) -> tuple[Path, Path]:
    return (generate_batch_plot(csv_path, filter_config),
            generate_condition_plot(csv_path.parent, filter_config))
