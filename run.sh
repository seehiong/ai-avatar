#!/usr/bin/env bash
# Set up (first run) and start the backend, which also serves the web client.
# Then open http://127.0.0.1:8080. Extra args go to server.py (e.g. --model qwen3.8:27b).
# --photoreal also starts the photoreal avatar service (tools/photoreal, needs the GPU env from its README
# section; set AVATAR_GPU_PY to its python if it isn't ~/miniforge3/envs/avatar-gpu).
# PHOTOREAL_ARGS tunes it. The default keeps the GPU load moderate (this box reached 92 C / 133 W and once
# hard-reset with the LLM and the face both at full load); "--idle-stride 2 --talk-stride 4" is smoother.
PHOTOREAL_ARGS="${PHOTOREAL_ARGS:---idle-stride 3 --talk-stride 5}"
set -euo pipefail
cd "$(dirname "$0")"

PHOTOREAL=0; ARGS=()
for a in "$@"; do if [ "$a" = "--photoreal" ]; then PHOTOREAL=1; else ARGS+=("$a"); fi; done
set -- "${ARGS[@]+"${ARGS[@]}"}"

if [ ! -d backend/.venv ]; then
  python3 -m venv backend/.venv
  backend/.venv/bin/pip install -r backend/requirements.txt
fi
if [ ! -f backend/voices/en_US-lessac-medium.onnx ]; then   # Piper: fallback voice
  backend/.venv/bin/python -m piper.download_voices --data-dir backend/voices en_US-lessac-medium
fi
if [ ! -f backend/voices/kokoro/model.onnx ]; then   # Kokoro-82M (Apache-2.0): default, more natural voice
  mkdir -p backend/voices/kokoro
  H=https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX/resolve/main
  curl -fL -o backend/voices/kokoro/model.onnx $H/onnx/model.onnx || echo "Kokoro download failed; Piper will be used"
  for v in am_michael am_adam am_eric am_liam am_puck bm_george; do curl -fsL -o backend/voices/kokoro/$v.bin $H/voices/$v.bin; done
fi
if [ ! -f backend/models/wav2arkit/wav2arkit_cpu.onnx.data ]; then   # neural lip sync (Apache-2.0, ~400 MB)
  mkdir -p backend/models/wav2arkit
  W=https://huggingface.co/myned-ai/wav2arkit_cpu/resolve/main
  for f in wav2arkit_cpu.onnx wav2arkit_cpu.onnx.data config.json; do
    curl -fL -o backend/models/wav2arkit/$f $W/$f || { echo "wav2arkit download failed; phoneme visemes will be used"; break; }
  done
fi

if [ "$PHOTOREAL" = 1 ]; then
  PY="${AVATAR_GPU_PY:-$HOME/miniforge3/envs/avatar-gpu/bin/python}"
  echo "Starting photoreal avatar service (ws://127.0.0.1:8766) with $PY"
  SENSORS="$PWD/photoreal-sensors.log"
  # shellcheck disable=SC2086
  (cd tools/photoreal && TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1 exec "$PY" photoreal_server.py $PHOTOREAL_ARGS --sensor-log "$SENSORS") &
  PHOTO_PID=$!
  trap 'kill $PHOTO_PID 2>/dev/null' EXIT INT TERM
  echo "Starting backend - open http://127.0.0.1:8080?avatar=photoreal once it says 'listening'"
  cd backend && .venv/bin/python server.py "$@"
  exit
fi

echo "Starting backend - open http://127.0.0.1:8080 once it says 'listening'"
cd backend && exec .venv/bin/python server.py "$@"
