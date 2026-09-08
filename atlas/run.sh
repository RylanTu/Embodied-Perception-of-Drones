#!/usr/bin/env bash
set -eo pipefail
ASCEND_ENV=/usr/local/Ascend/ascend-toolkit/set_env.sh
if [[ -f "$ASCEND_ENV" ]]; then
  source "$ASCEND_ENV"
fi
set -u
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
if [[ -n "${WHISKER_NPU_DIR:-}" ]]; then
  export PYTHONPATH="${WHISKER_NPU_DIR}:${PYTHONPATH:-}"
fi
python3 service.py "$@"
