from __future__ import annotations

from html import escape
from pathlib import Path

import numpy as np
import pandas as pd

from analyze_20260829_0929_0931 import FILES, paired_leave_out, resample
from compare_20260829 import GRID, moving_average


OUT = Path(__file__).parent / "output" / "20260829_092929_093114_obstacle_comparison.svg"
WIDTH, HEIGHT = 1600, 900
SERIES = [
    (FILES[0], "无障碍 · 092929", "#277da1"),
    (FILES[1], "有障碍 · 093114", "#e45756"),
]
FEATURE_START_MM = 1600
FEATURE_END_MM = 1770
FEATURE_NAME = "晚段斜坡"
FOOTNOTE = None


def load(path: Path) -> np.ndarray:
    df = pd.read_csv(path)
    return np.vstack([moving_average(resample(trial), 5) for _, trial in df.groupby("trial_id", sort=True)])


def path_data(x, y, sx, sy):
    return " ".join(("M" if i == 0 else "L") + f" {sx(a):.2f} {sy(b):.2f}" for i, (a, b) in enumerate(zip(x, y)))


def polygon_data(x, upper, lower, sx, sy):
    pts = [(sx(a), sy(b)) for a, b in zip(x, upper)]
    pts += [(sx(a), sy(b)) for a, b in zip(x[::-1], lower[::-1])]
    return " ".join(f"{a:.2f},{b:.2f}" for a, b in pts)


def text(x, y, value, size=20, color="#172033", weight=400, anchor="start"):
    return (
        f'<text x="{x}" y="{y}" font-family="Microsoft YaHei, Segoe UI, sans-serif" '
        f'font-size="{size}" font-weight="{weight}" fill="{color}" text-anchor="{anchor}">{escape(str(value))}</text>'
    )


def main():
    datasets = [(name, color, load(path)) for path, name, color in SERIES]
    all_curves = np.vstack([curves for _, _, curves in datasets])
    clear_mean = datasets[0][2].mean(axis=0)
    obstacle_mean = datasets[1][2].mean(axis=0)
    gap = obstacle_mean - clear_mean
    max_idx = int(np.argmax(np.abs(gap)))
    rms_gap = float(np.sqrt(np.mean(gap ** 2)))
    i0 = int(np.argmin(np.abs(GRID - FEATURE_START_MM)))
    i1 = int(np.argmin(np.abs(GRID - FEATURE_END_MM)))
    ramp_clear = datasets[0][2][:, i1] - datasets[0][2][:, i0]
    ramp_obstacle = datasets[1][2][:, i1] - datasets[1][2][:, i0]
    ramp_correct = 0
    for index in range(min(len(ramp_clear), len(ramp_obstacle))):
        mean_clear = np.delete(ramp_clear, index).mean()
        mean_obstacle = np.delete(ramp_obstacle, index).mean()
        threshold = (mean_clear + mean_obstacle) / 2
        direction = 1 if mean_obstacle > mean_clear else -1
        ramp_correct += int(direction * (ramp_clear[index] - threshold) <= 0)
        ramp_correct += int(direction * (ramp_obstacle[index] - threshold) > 0)
    ramp_accuracy = ramp_correct / (2 * min(len(ramp_clear), len(ramp_obstacle)))
    full_accuracy, _, _ = paired_leave_out(datasets[0][2], datasets[1][2])
    ymin = float(np.floor((all_curves.min() - 0.08) * 2) / 2)
    ymax = float(np.ceil((all_curves.max() + 0.08) * 2) / 2)

    px, py, pw, ph = 80, 155, 1440, 625
    left, top, plot_w, plot_h = px + 92, py + 55, pw - 135, ph - 135
    sx = lambda value: left + (value - GRID[0]) / (GRID[-1] - GRID[0]) * plot_w
    sy = lambda value: top + (ymax - value) / (ymax - ymin) * plot_h

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        '<rect width="100%" height="100%" fill="#f7f9fc"/>',
        text(80, 68, "障碍物压力波形对比", 34, "#172033", 700),
        text(80, 105, f"{datasets[0][0]} · {datasets[1][0]} · 通道1 · 5点平滑 · 阴影为 ±1 标准差", 18, "#657085"),
        f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="18" fill="#ffffff" stroke="#e7ebf2"/>',
    ]
    for i, (name, color, _) in enumerate(datasets):
        lx = 1050 + i * 245
        svg += [
            f'<line x1="{lx}" y1="101" x2="{lx+44}" y2="101" stroke="{color}" stroke-width="3"/>',
            text(lx + 55, 108, name, 18, "#172033", 500),
        ]

    for xv in (20, 400, 800, 1200, 1600, 1880):
        xx = sx(xv)
        svg += [
            f'<line x1="{xx:.2f}" y1="{top}" x2="{xx:.2f}" y2="{top+plot_h}" stroke="#dfe5ee" stroke-width="1"/>',
            text(xx, top + plot_h + 33, xv, 16, "#657085", anchor="middle"),
        ]
    for yv in np.arange(np.ceil(ymin * 2) / 2, ymax + 0.01, 0.5):
        yy = sy(yv)
        svg += [
            f'<line x1="{left}" y1="{yy:.2f}" x2="{left+plot_w}" y2="{yy:.2f}" stroke="#dfe5ee" stroke-width="1"/>',
            text(left - 15, yy + 6, f"{yv:.1f}", 16, "#657085", anchor="end"),
        ]

    for _, color, curves in datasets:
        mean = curves.mean(axis=0)
        std = curves.std(axis=0, ddof=1)
        svg += [
            f'<polygon points="{polygon_data(GRID, mean+std, mean-std, sx, sy)}" fill="{color}" opacity="0.13"/>',
            f'<path d="{path_data(GRID, mean, sx, sy)}" fill="none" stroke="{color}" stroke-width="2.4" stroke-linejoin="round"/>',
        ]

    marker_x = sx(GRID[max_idx])
    box_x = max(left + 10, min(marker_x - 150, left + plot_w - 310))
    svg += [
        f'<line x1="{marker_x:.2f}" y1="{top}" x2="{marker_x:.2f}" y2="{top+plot_h}" stroke="#8d97a8" stroke-width="1.2"/>',
        f'<rect x="{box_x:.2f}" y="{top+15}" width="300" height="55" rx="9" fill="#ffffff" opacity="0.93" stroke="#dfe5ee"/>',
        text(box_x + 15, top + 49, f"最大均值差 {GRID[max_idx]:.0f} mm · {abs(gap[max_idx]):.3f} Pa", 16, "#657085", 500),
        text(left + plot_w / 2, top + plot_h + 72, "滑台距离 (mm)", 18, "#172033", 500, "middle"),
        f'<text x="112" y="{top+plot_h/2}" transform="rotate(-90 112 {top+plot_h/2})" font-family="Microsoft YaHei, Segoe UI, sans-serif" font-size="18" font-weight="500" fill="#172033" text-anchor="middle">压力 (Pa)</text>',
        text(80, 835, FOOTNOTE or f"两组各{len(datasets[0][2])}次实验；RMS差异 {rms_gap:.3f} Pa；完整波形留一验证 {full_accuracy*100:.1f}%；{FEATURE_NAME} {ramp_accuracy*100:.1f}%。", 16, "#657085"),
        "</svg>",
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(svg), encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
