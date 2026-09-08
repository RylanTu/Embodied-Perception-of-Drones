from __future__ import annotations

from html import escape
from pathlib import Path

import numpy as np

from compare_20260829 import (
    GRID,
    difference_metrics,
    leave_pair_out_accuracy,
    load_trials,
)


OUT = Path(__file__).parent / "output" / "20260829_fan_obstacle_comparison.svg"
WIDTH, HEIGHT = 1600, 900

BG = "#f7f9fc"
PANEL = "#ffffff"
TEXT = "#172033"
MUTED = "#657085"
GRID_COLOR = "#dfe5ee"
OBSTACLE = "#e45756"
CLEAR = "#277da1"


def points_path(x, y, sx, sy):
    return " ".join(("M" if i == 0 else "L") + f" {sx(a):.2f} {sy(b):.2f}" for i, (a, b) in enumerate(zip(x, y)))


def polygon_points(x, upper, lower, sx, sy):
    pts = [(sx(a), sy(b)) for a, b in zip(x, upper)]
    pts += [(sx(a), sy(b)) for a, b in zip(x[::-1], lower[::-1])]
    return " ".join(f"{a:.2f},{b:.2f}" for a, b in pts)


def text(x, y, value, size=20, fill=TEXT, weight=400, anchor="start"):
    return (
        f'<text x="{x}" y="{y}" font-family="Microsoft YaHei, Segoe UI, sans-serif" '
        f'font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{escape(str(value))}</text>'
    )


def main():
    rows = load_trials()
    all_curves = np.vstack([r["curve_filtered"] for r in rows])
    ymin = float(np.floor((all_curves.min() - 0.08) * 2) / 2)
    ymax = float(np.ceil((all_curves.max() + 0.08) * 2) / 2)

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        f'<rect width="100%" height="100%" fill="{BG}"/>',
        text(80, 68, "障碍物压力波形对比", 34, TEXT, 700),
        text(80, 104, "按滑台距离对齐 · 通道1 · 5点平滑 · 阴影为 ±1 标准差", 18, MUTED),
    ]

    legend_y = 102
    svg += [
        f'<line x1="1110" y1="{legend_y-6}" x2="1150" y2="{legend_y-6}" stroke="{OBSTACLE}" stroke-width="3"/>',
        text(1160, legend_y, "有障碍", 17, TEXT),
        f'<line x1="1280" y1="{legend_y-6}" x2="1320" y2="{legend_y-6}" stroke="{CLEAR}" stroke-width="3"/>',
        text(1330, legend_y, "无障碍", 17, TEXT),
    ]

    panel_specs = [
        (70, 145, 710, 635, "fan", "有风扇"),
        (820, 145, 710, 635, "no_fan", "无风扇"),
    ]

    for px, py, pw, ph, wind, title in panel_specs:
        plot_left, plot_top = px + 76, py + 76
        plot_w, plot_h = pw - 110, ph - 145
        sx = lambda value: plot_left + (value - GRID[0]) / (GRID[-1] - GRID[0]) * plot_w
        sy = lambda value: plot_top + (ymax - value) / (ymax - ymin) * plot_h

        obstacle = np.vstack([r["curve_filtered"] for r in rows if r["wind"] == wind and r["label"] == 1])
        clear = np.vstack([r["curve_filtered"] for r in rows if r["wind"] == wind and r["label"] == 0])
        om, os = obstacle.mean(axis=0), obstacle.std(axis=0, ddof=1)
        cm, cs = clear.mean(axis=0), clear.std(axis=0, ddof=1)
        metrics = difference_metrics(rows, wind, "curve_filtered")
        accuracy, _, _ = leave_pair_out_accuracy(rows, "curve_filtered", wind)

        svg += [
            f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="18" fill="{PANEL}" stroke="#e7ebf2"/>',
            text(px + 30, py + 42, title, 25, TEXT, 700),
            text(px + pw - 30, py + 41, f"留一验证 {accuracy*100:.1f}%", 17, MUTED, 500, "end"),
        ]

        for xv in (20, 400, 800, 1200, 1600, 1880):
            xx = sx(xv)
            svg.append(f'<line x1="{xx:.2f}" y1="{plot_top}" x2="{xx:.2f}" y2="{plot_top+plot_h}" stroke="{GRID_COLOR}" stroke-width="1"/>')
            svg.append(text(xx, plot_top + plot_h + 31, xv, 15, MUTED, anchor="middle"))
        yticks = np.arange(np.ceil(ymin * 2) / 2, ymax + 0.01, 0.5)
        for yv in yticks:
            yy = sy(yv)
            svg.append(f'<line x1="{plot_left}" y1="{yy:.2f}" x2="{plot_left+plot_w}" y2="{yy:.2f}" stroke="{GRID_COLOR}" stroke-width="1"/>')
            svg.append(text(plot_left - 13, yy + 5, f"{yv:.1f}", 15, MUTED, anchor="end"))

        svg += [
            f'<polygon points="{polygon_points(GRID, om+os, om-os, sx, sy)}" fill="{OBSTACLE}" opacity="0.13"/>',
            f'<polygon points="{polygon_points(GRID, cm+cs, cm-cs, sx, sy)}" fill="{CLEAR}" opacity="0.13"/>',
            f'<path d="{points_path(GRID, om, sx, sy)}" fill="none" stroke="{OBSTACLE}" stroke-width="2.2" stroke-linejoin="round"/>',
            f'<path d="{points_path(GRID, cm, sx, sy)}" fill="none" stroke="{CLEAR}" stroke-width="2.2" stroke-linejoin="round"/>',
        ]

        marker_x = sx(metrics["max_effect_distance_mm"])
        svg += [
            f'<line x1="{marker_x:.2f}" y1="{plot_top}" x2="{marker_x:.2f}" y2="{plot_top+plot_h}" stroke="#8d97a8" stroke-width="1.2"/>',
            f'<rect x="{max(plot_left+8, min(marker_x-104, plot_left+plot_w-216)):.2f}" y="{plot_top+12}" width="208" height="48" rx="8" fill="#ffffff" opacity="0.92" stroke="#dfe5ee"/>',
            text(max(plot_left+18, min(marker_x-94, plot_left+plot_w-206)), plot_top + 42,
                 f"最大差异 {metrics['max_effect_distance_mm']:.0f} mm · d={metrics['max_abs_cohen_d']:.2f}", 15, MUTED, 500),
            text(plot_left + plot_w / 2, plot_top + plot_h + 61, "滑台距离 (mm)", 17, TEXT, 500, "middle"),
        ]

        ylabel_x = px + 27
        ylabel_y = plot_top + plot_h / 2
        svg.append(
            f'<text x="{ylabel_x}" y="{ylabel_y}" transform="rotate(-90 {ylabel_x} {ylabel_y})" '
            f'font-family="Microsoft YaHei, Segoe UI, sans-serif" font-size="17" font-weight="500" fill="{TEXT}" text-anchor="middle">压力 (Pa)</text>'
        )

    svg += [
        text(80, 833, "说明：每类20次实验；图中标签按用户给出的文件含义覆盖CSV内旧默认标签。当前4份数据仅通道1有效。", 16, MUTED),
        "</svg>",
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(svg), encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
