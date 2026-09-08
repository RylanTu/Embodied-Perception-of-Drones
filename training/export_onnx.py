#!/usr/bin/env python3
"""Re-export pressure_ramp.pt as a static Atlas-compatible ONNX model."""

from __future__ import annotations

import argparse
from pathlib import Path

import torch

from train import RampNet


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("--output", type=Path, default=Path("pressure_ramp.onnx"))
    args = parser.parse_args()

    checkpoint = torch.load(args.checkpoint, map_location="cpu", weights_only=True)
    state = checkpoint["state_dict"]
    model = RampNet(state["feature_mean"].reshape(-1),
                    state["feature_std"].reshape(-1))
    model.load_state_dict(state)
    model.eval()

    window = int(checkpoint["window_samples"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    dummy = torch.zeros(1, 1, 1, window, dtype=torch.float32)
    torch.onnx.export(model, dummy, args.output,
                      input_names=["pressure"], output_names=["logit"],
                      opset_version=13, dynamo=False, external_data=False)
    print(f"static ONNX written: {args.output} input=[1,1,1,{window}]")


if __name__ == "__main__":
    main()
