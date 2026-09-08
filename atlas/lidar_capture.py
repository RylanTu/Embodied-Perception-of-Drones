#!/usr/bin/env python3
"""Capture complete RPLIDAR revolutions and optionally run a calibrated ROI decision."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import json
from pathlib import Path
import time

from lidar_workflow import (AcquisitionConfig, LidarDecisionWorkflow,
                            RoiOccupancyDecision, WorkflowState)


CSV_COLUMNS = ("revolution", "point", "timestamp_s", "angle_deg", "distance_mm",
               "quality", "rail_position_m", "x_m", "y_m", "z_m")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True,
                        help="RPLIDAR serial port; use /dev/ttyUSB1 on Atlas only after checking it")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--pitch-deg", type=float, default=45.0)
    parser.add_argument("--warmup-revolutions", type=int, default=2)
    parser.add_argument("--sample-revolutions", type=int, default=5)
    parser.add_argument("--minimum-points", type=int, default=80)
    parser.add_argument("--minimum-angle-coverage", type=float, default=270.0)
    parser.add_argument("--minimum-distance-mm", type=float, default=50.0)
    parser.add_argument("--maximum-distance-mm", type=float, default=6000.0)
    parser.add_argument("--data-timeout-s", type=float, default=2.0)
    parser.add_argument("--session-timeout-s", type=float, default=20.0)
    parser.add_argument("--rail-origin-m", type=float, default=0.0)
    parser.add_argument("--rail-speed-m-s", type=float, default=0.0,
                        help="open-loop fallback only; production integration should supply measured position")
    parser.add_argument("--roi", type=float, nargs=5, metavar=("X_MIN", "X_MAX", "Y_ABS", "Z_MIN", "Z_MAX"),
                        help="enable geometry decision with calibrated world-coordinate ROI in metres")
    parser.add_argument("--minimum-roi-hits", type=int, default=3)
    parser.add_argument("--minimum-roi-revolutions", type=int, default=3)
    parser.add_argument("--minimum-roi-hit-ratio", type=float, default=.01)
    parser.add_argument("--output", type=Path,
                        help="point CSV path; default data/lidar/<timestamp>.csv")
    return parser


def save_result(workflow: LidarDecisionWorkflow, output: Path, status: dict) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(CSV_COLUMNS)
        for scan_index, scan in enumerate(workflow.scans, 1):
            for point_index, point in enumerate(scan, 1):
                writer.writerow((scan_index, point_index, point.timestamp_s,
                                 point.angle_deg, point.distance_mm, point.quality,
                                 point.rail_position_m, point.x_m, point.y_m, point.z_m))
    output.with_suffix(".json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def main() -> int:
    args = build_parser().parse_args()
    config = AcquisitionConfig(
        pitch_deg=args.pitch_deg,
        minimum_distance_mm=args.minimum_distance_mm,
        maximum_distance_mm=args.maximum_distance_mm,
        minimum_points_per_revolution=args.minimum_points,
        minimum_angle_coverage_deg=args.minimum_angle_coverage,
        warmup_revolutions=args.warmup_revolutions,
        sample_revolutions=args.sample_revolutions,
        data_timeout_s=args.data_timeout_s,
        session_timeout_s=args.session_timeout_s,
    )
    decision = None
    if args.roi:
        decision = RoiOccupancyDecision(*args.roi,
            minimum_hits_per_revolution=args.minimum_roi_hits,
            minimum_occupied_revolutions=args.minimum_roi_revolutions,
            minimum_total_hit_ratio=args.minimum_roi_hit_ratio)
    workflow = LidarDecisionWorkflow(config, decision)
    output = args.output or Path("data/lidar") / (datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv")

    import serial
    device = serial.Serial(args.port, args.baud, timeout=.05)
    try:
        device.write(workflow.stop())
        time.sleep(.2)
        device.reset_input_buffer()
        started = time.monotonic()
        device.write(workflow.start(started))
        print("RPLIDAR warming started")
        previous_state = workflow.state
        while workflow.state in workflow.ACTIVE:
            now = time.monotonic()
            data = device.read(min(max(device.in_waiting, 1), 4096))
            position = args.rail_origin_m + args.rail_speed_m_s * (now - started)
            workflow.feed(data, now, position)
            workflow.poll(now)
            if workflow.state != previous_state:
                print(f"state: {previous_state.value} -> {workflow.state.value}")
                previous_state = workflow.state
    except KeyboardInterrupt:
        workflow.stop()
        print("capture stopped by user")
    finally:
        try:
            device.write(workflow.stop())
        finally:
            device.close()

    status = workflow.public_status()
    save_result(workflow, output, status)
    print(json.dumps({"csv": str(output), "status": status}, ensure_ascii=False, indent=2))
    return 0 if workflow.state in (WorkflowState.COMPLETE, WorkflowState.STOPPED) else 2


if __name__ == "__main__":
    raise SystemExit(main())
