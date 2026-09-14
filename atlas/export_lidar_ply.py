#!/usr/bin/env python3
"""Export saved point CSVs to an ASCII PLY without loading the cloud into RAM."""
import argparse
import csv
import math
from pathlib import Path
import shutil
import tempfile


def export_ply(source: Path, output: Path) -> int:
    files = sorted(source.glob("points_*.csv")) if source.is_dir() else [source]
    if not files:
        raise ValueError("没有找到 points_*.csv 点云文件")
    count = 0
    # Spool once so vertex count matches exactly; use disk, not unbounded RAM.
    with tempfile.TemporaryFile(mode="w+", encoding="ascii", newline="\n") as vertices:
        for path in files:
            with path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                if not {"x_m", "y_m", "z_m", "quality"}.issubset(reader.fieldnames or []):
                    raise ValueError(f"不是点云 CSV：{path}")
                for row in reader:
                    xyz = [float(row[key]) for key in ("x_m", "y_m", "z_m")]
                    quality = int(row["quality"])
                    if not all(math.isfinite(v) for v in xyz) or not 0 <= quality <= 255:
                        raise ValueError(f"无效点云行：{path}:{reader.line_num}")
                    vertices.write(" ".join(map(str, xyz)) + f" {quality}\n")
                    count += 1
        vertices.seek(0)
        with output.open("x", encoding="ascii", newline="\n") as target:
            target.write("ply\nformat ascii 1.0\ncomment coordinates in metres\n"
                         f"element vertex {count}\nproperty double x\nproperty double y\n"
                         "property double z\nproperty uchar quality\nend_header\n")
            shutil.copyfileobj(vertices, target)
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="One session directory or one point CSV")
    parser.add_argument("output", type=Path, help="New .ply file; existing files are never overwritten")
    args = parser.parse_args()
    print(f"Exported {export_ply(args.source, args.output)} points to {args.output}")
