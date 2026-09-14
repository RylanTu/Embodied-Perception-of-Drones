#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MODEL_ONNX="$SCRIPT_DIR/models/pressure_causal_tcn_ch234.onnx"
MODEL_PREFIX="$SCRIPT_DIR/models/pressure_causal_tcn_ch234"
MODEL_BUILD="${MODEL_PREFIX}.building.$$"
CONFIG_PATH="${1:-$SCRIPT_DIR/config.json}"
SOC_VERSION="${2:-Ascend310B4}"
ASCEND_ENV="/usr/local/Ascend/ascend-toolkit/set_env.sh"

if [[ "${FORCE_ATC:-0}" == "1" || ! -s "${MODEL_PREFIX}.om" ]]; then
  if [[ ! -f "$ASCEND_ENV" ]]; then
    echo "未找到CANN环境脚本: $ASCEND_ENV" >&2
    exit 1
  fi
  if [[ ! -s "$MODEL_ONNX" ]]; then
    echo "未找到ONNX模型: $MODEL_ONNX" >&2
    exit 1
  fi

  source "$ASCEND_ENV"
  command -v atc >/dev/null 2>&1 || { echo "当前CANN环境没有atc命令" >&2; exit 1; }

  atc \
    --model="$MODEL_ONNX" \
    --framework=5 \
    --output="$MODEL_BUILD" \
    --input_format=NCHW \
    --input_shape="pressure:1,3,1,200" \
    --soc_version="$SOC_VERSION"

  if [[ ! -s "${MODEL_BUILD}.om" ]]; then
    echo "ATC未生成有效OM文件" >&2
    exit 1
  fi
  mv -f "${MODEL_BUILD}.om" "${MODEL_PREFIX}.om"
else
  echo "复用现有OM模型: ${MODEL_PREFIX}.om"
fi

python3 - "$CONFIG_PATH" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
try:
    config = json.loads(path.read_text(encoding="utf-8"))
except FileNotFoundError:
    config = {}
model = config.setdefault("model", {})
model.update({
    "enabled": True,
    "path": "models/pressure_causal_tcn_ch234.om",
    "device_id": 0,
    "window_samples": 200,
    "input_channels": 3,
    "channels": [2, 3, 4],
    "probability_threshold": 0.885,
    "consecutive_hits": 3,
    "hold_ms": 300,
    "stop_on_obstacle": True,
    "stop_in_test_mode": False,
    "start_ignore_extra_mm": 100.0,
})
path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
PY

echo "模型已就绪: ${MODEL_PREFIX}.om"
echo "配置已更新: $CONFIG_PATH"
echo "请重新启动服务: ./run.sh --host 0.0.0.0 --port 8080"
