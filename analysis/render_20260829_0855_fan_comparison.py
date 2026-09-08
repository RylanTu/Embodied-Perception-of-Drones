from __future__ import annotations

from html import escape
from pathlib import Path

import numpy as np
import pandas as pd


FILES = [
    (Path(r"D:\聊天文件\微信\xwechat_files\wxid_1dpbeiucwjug22_2a10\msg\file\2026-08\20260829T085541_batch_001.csv"), "无风扇", "#277da1"),
    (Path(r"D:\聊天文件\微信\xwechat_files\wxid_1dpbeiucwjug22_2a10\msg\file\2026-08\20260829T085856_batch_001.csv"), "有风扇", "#e45756"),
]
GRID = np.arange(20.0, 1880.1, 10.0)
OUT = Path(__file__).parent / "output" / "20260829_085541_085856_fan_comparison.svg"
WIDTH, HEIGHT = 1600, 900


def smooth(x: np.ndarray, width: int = 5) -> np.ndarray:
    xp = np.pad(x, (width // 2, width - 1 - width // 2), mode="edge")
    return np.convolve(xp, np.ones(width) / width, mode="valid")


def load_curves(path: Path) -> np.ndarray:
    df = pd.read_csv(path)
    curves = []
    for _, trial in df.groupby("trial_id", sort=True):
        d = trial.loc[
            (trial["phase"] == "measure")
            & trial["position_mm"].notna()
            & trial["pressure_pa_1"].notna()
            & trial["valid_mask"].astype(int).map(lambda v: bool(v & 1)),
            ["position_mm", "pressure_pa_1"],
        ]
        d = d.groupby("position_mm", as_index=False)["pressure_pa_1"].mean().sort_values("position_mm")
        curve = np.interp(GRID, d["position_mm"].to_numpy(float), d["pressure_pa_1"].to_numpy(float))
        curves.append(smooth(curve))
    return np.vstack(curves)


def path_data(x, y, sx, sy):
    return " ".join(("M" if i == 0 else "L") + f" {sx(a):.2f} {sy(b):.2f}" for i, (a, b) in enumerate(zip(x, y)))


def polygon_data(x, upper, lower, sx, sy):
    pts = [(sx(a), sy(b)) for a, b in zip(x, upper)]
    pts += [(sx(a), sy(b)) for a, b in zip(x[::-1], lower[::-1])]
    return " ".join(f"{a:.2f},{b:.2f}" for a, b in pts)


def label(x, y, value, size=20, color="#172033", weight=400, anchor="start"):
    return (
        f'<text x="{x}" y="{y}" font-family="Microsoft YaHei, Segoe UI, sans-serif" '
        f'font-size="{size}" font-weight="{weight}" fill="{color}" text-anchor="{anchor}">{escape(str(value))}</text>'
    )


def main():
    datasets = [(name, color, load_curves(path)) for path, name, color in FILES]
    all_curves = np.vstack([curves for _, _, curves in datasets])
    means = [curves.mean(axis=0) for _, _, curves in datasets]
    gap = means[1] - means[0]
    gap_idx = int(np.argmax(np.abs(gap)))
    rms_gap = float(np.sqrt(np.mean(gap ** 2)))

    ymin = float(np.floor((all_curves.min() - 0.08) * 2) / 2)
    ymax = float(np.ceil((all_curves.max() + 0.08) * 2) / 2)
    px, py, pw, ph = 80, 155, 1440, 625
    left, top, plot_w, plot_h = px + 92, py + 55, pw - 135, ph - 135
    sx = lambda value: left + (value - GRID[0]) / (GRID[-1] - GRID[0]) * plot_w
    sy = lambda value: top + (ymax - value) / (ymax - ymin) * plot_h

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        '<rect width="100%" height="100%" fill="#f7f9fc"/>',
        label(80, 68, "风扇工况压力波形对比", 34, "#172033", 700),
        label(80, 105, "085541 无风扇 · 085856 有风扇 · 通道1 · 5点平滑 · 阴影为 ±1 标准差", 18, "#657085"),
        f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="18" fill="#ffffff" stroke="#e7ebf2"/>',
    ]

    for i, (name, color, curves) in enumerate(datasets):
        lx = 1100 + i * 190
        svg += [
            f'<line x1="{lx}" y1="101" x2="{lx+44}" y2="101" stroke="{color}" stroke-width="3"/>',
            label(lx + 55, 108, name, 18, "#172033", 500),
        ]

    for xv in (20, 400, 800, 1200, 1600, 1880):
        xx = sx(xv)
        svg += [
            f'<line x1="{xx:.2f}" y1="{top}" x2="{xx:.2f}" y2="{top+plot_h}" stroke="#dfe5ee" stroke-width="1"/>',
            label(xx, top + plot_h + 33, xv, 16, "#657085", anchor="middle"),
        ]
    for yv in np.arange(np.ceil(ymin * 2) / 2, ymax + 0.01, 0.5):
        yy = sy(yv)
        svg += [
            f'<line x1="{left}" y1="{yy:.2f}" x2="{left+plot_w}" y2="{yy:.2f}" stroke="#dfe5ee" stroke-width="1"/>',
            label(left - 15, yy + 6, f"{yv:.1f}", 16, "#657085", anchor="end"),
        ]

    for name, color, curves in datasets:
        mean = curves.mean(axis=0)
        std = curves.std(axis=0, ddof=1)
        svg += [
            f'<polygon points="{polygon_data(GRID, mean+std, mean-std, sx, sy)}" fill="{color}" opacity="0.13"/>',
            f'<path d="{path_data(GRID, mean, sx, sy)}" fill="none" stroke="{color}" stroke-width="2.4" stroke-linejoin="round"/>',
        ]

    marker_x = sx(GRID[gap_idx])
    box_x = max(left + 10, min(marker_x - 150, left + plot_w - 310))
    svg += [
        f'<line x1="{marker_x:.2f}" y1="{top}" x2="{marker_x:.2f}" y2="{top+plot_h}" stroke="#8d97a8" stroke-width="1.2"/>',
        f'<rect x="{box_x:.2f}" y="{top+15}" width="300" height="55" rx="9" fill="#ffffff" opacity="0.93" stroke="#dfe5ee"/>',
        label(box_x + 15, top + 49, f"最大均值差 {GRID[gap_idx]:.0f} mm · {abs(gap[gap_idx]):.3f} Pa", 16, "#657085", 500),
        label(left + plot_w / 2, top + plot_h + 72, "滑台距离 (mm)", 18, "#172033", 500, "middle"),
        f'<text x="112" y="{top+plot_h/2}" transform="rotate(-90 112 {top+plot_h/2})" font-family="Microsoft YaHei, Segoe UI, sans-serif" font-size="18" font-weight="500" fill="#172033" text-anchor="middle">压力 (Pa)</text>',
        label(80, 835, f"两组各20次实验；全程均值曲线 RMS 差异 {rms_gap:.3f} Pa。CSV内部仍为旧默认 nofan 标签，图中风扇状态按用户说明覆盖。", 16, "#657085"),
        "</svg>",
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(svg), encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
