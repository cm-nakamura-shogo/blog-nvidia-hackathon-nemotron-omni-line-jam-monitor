#!/usr/bin/env bash
set -euo pipefail
# Jetson AGX Orin で実行する。VLM を LAN に OpenAI 互換 API（モデル名 line-base）で出す。
# GGUF は先に ~/llama-cache へ置いておく（README.md の初回構築）。
# 既存のコンテナは消さない。同名のコンテナがあれば docker run が失敗して止まる。
model_dir="$HOME/llama-cache"
# --network host: 監視側の Jetson から LAN の <このJetsonのIP>:8001 で直接呼ぶため。
#   閉じた LAN に置く前提で、認証は付けていない。
# --alias line-base: RTX と同じモデル名にして、monitor の --model を変えずに切り替えるため。
# -ngl 999: 全層を GPU に載せる。Orin は CPU と GPU でメモリを共有するので、CPU に残す利点がない。
# -c 16384 --parallel 2: monitor は 👍 と障害物の 2 要求を同時に送る。1 スロット 8192 にして、2 本を待たせずに受ける。
# mmproj は画像の入力に要る。
sudo docker run -d --name line-base-orin --restart unless-stopped \
  --runtime=nvidia --network host \
  -v "$model_dir:/models:ro" \
  ghcr.io/nvidia-ai-iot/llama_cpp:latest-jetson-orin \
  llama-server \
    -m /models/NVIDIA-Nemotron-3-Nano-Omni-30B-A3B-Q8_0.gguf \
    --mmproj /models/mmproj-NVIDIA-Nemotron-3-Nano-Omni-30B-A3B-Q8_0.gguf \
    --alias line-base -ngl 999 -fa on --jinja \
    -c 16384 --parallel 2 \
    --host 0.0.0.0 --port 8001
