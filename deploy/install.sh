#!/usr/bin/env bash
set -euo pipefail
# 監視側の Jetson で、一般ユーザー（sudo なし）として実行する。カメラ（camera-preview）と詰まり監視（line-monitor）を systemd に入れ、自動起動する。
# 先にこのフォルダを ~/line-monitor に置き、deploy/*.service の伏せ字（<USER> など）を自分の環境に合わせて書き換えておく。
if [[ $(id -u) == 0 ]]; then
  echo '一般ユーザーとして実行してください（sudo を付けない）。'; exit 1
fi
if [[ $# != 1 || ! -c "$1" ]]; then
  echo '使い方: bash deploy/install.sh /dev/v4l/by-id/<カメラ>-video-index0'; exit 1
fi
monitor_dir="$(cd -- "$(dirname -- "$0")/.." && pwd)"
if [[ "$monitor_dir" != "$HOME/line-monitor" ]]; then
  echo '先にこのフォルダを ~/line-monitor に置いてください。'; exit 1
fi
cd "$monitor_dir"
if grep -q -e '<USER>' -e '<CONVEYOR_MCP_IP>' -e '<VLM_IP>' -e '<MQTT_IP>' deploy/*.service; then
  echo 'deploy/*.service の伏せ字を先に書き換えてください。'; exit 1
fi
# systemd の EnvironmentFile に書けない文字は受け付けない
if [[ "$1" == *'"'* || "$1" == *$'\n'* || "$1" == *'\'* || "$1" == *'`'* || "$1" == *'$'* || "$1" == *' '* ]]; then
  echo '使えない文字を含むカメラのパスです'; exit 1
fi
# 手で起動した監視やカメラが残っていると、サービスが同じポートや保存先の鍵を取れずに起動を繰り返す
if pgrep -u "$(id -un)" -f 'line-monitor/(monitor|camera_preview)\.py' >/dev/null && ! systemctl is-active --quiet line-monitor camera-preview; then
  echo '手で起動した monitor.py か camera_preview.py が動いています。先に止めてください:'
  pgrep -a -u "$(id -un)" -f 'line-monitor/(monitor|camera_preview)\.py'
  exit 1
fi
[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt
sudo install -d -m 755 /etc/line-monitor
printf 'LINE_CAMERA=%s\n' "$1" | sudo sh -c 'cat > /etc/line-monitor/environment'
sudo install -m 644 deploy/camera-preview.service /etc/systemd/system/camera-preview.service
sudo install -m 644 deploy/line-monitor.service /etc/systemd/system/line-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable camera-preview line-monitor
sudo systemctl restart camera-preview line-monitor
sudo systemctl status camera-preview line-monitor --no-pager
