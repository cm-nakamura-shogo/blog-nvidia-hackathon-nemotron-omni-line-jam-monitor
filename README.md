# blog-nvidia-hackathon-nemotron-omni-line-jam-monitor

ベルトコンベアの詰まりをカメラと VLM（Nemotron 3 Nano Omni 30B-A3B）で見つけて止め、人の 👍 で再開する監視のサンプルです。VLM は Jetson AGX Orin 上の llama.cpp で動かします。

ブログ記事: <!-- TODO: 記事のURL -->

## 構成

```
カメラ → 監視側の Jetson（camera_preview.py :8081）
          └─ monitor.py（0.5 秒ごとに最新の 1 枚を取得）
               ├─ LAN → Jetson AGX Orin（llama-server :8001 で VLM 判定）
               ├─ 詰まり → コンベアの MCP サーバーで停止
               ├─ 👍 → コンベアの MCP サーバーで再開
               ├─ MQTT → 停止・再開を通知（任意）
               └─ 画面 :8082（状態・操作・ROI の調整）
```

コンベアの MCP サーバーは [blog-nvidia-hackathon-conveyor-mcp](https://github.com/cm-nakamura-shogo/blog-nvidia-hackathon-conveyor-mcp) を使います。

| ファイル | 内容 |
| --- | --- |
| `monitor.py` | 監視の本体（撮影・推論・制御のスレッド、起動待ち） |
| `detail_observation.py` | ROI の切り出しと、障害物（詰まり）のプロンプト |
| `observation_pipeline.py` | 👍 のプロンプトと、VLM の呼び出し |
| `line_events.py` | 詰まりを確定するルール |
| `alert_state.py` | 警報の保持と、👍 による解除のルール |
| `conveyor_control.py` | コンベアの MCP サーバーの呼び出し |
| `monitor_ui.py` | 監視画面 |
| `camera_preview.py` | カメラの映像を HTTP で配信する |
| `serve/run_orin_llama.sh` | Jetson AGX Orin で VLM（llama-server）を起動する |
| `deploy/` | systemd のユニットファイルと導入スクリプト |

## VLM の準備（Jetson AGX Orin）

`ggml-org/NVIDIA-Nemotron-3-Nano-Omni-30B-A3B-GGUF` の Q8_0 本体と mmproj を `~/llama-cache` に置き、MAXN モードで起動します。

```bash
sudo nvpmodel -m 0
sudo jetson_clocks
bash serve/run_orin_llama.sh
curl -fsS http://localhost:8001/health
```

## 監視の導入（監視側の Jetson）

1. このフォルダを `~/line-monitor` に置く
2. `deploy/*.service` の伏せ字（`<USER>`、`<CONVEYOR_MCP_IP>`、`<VLM_IP>`、`<MQTT_IP>`）を書き換える。MQTT を使わない場合は `--mqtt-host` を消す
3. 導入スクリプトを実行する

```bash
cd ~/line-monitor
bash deploy/install.sh /dev/v4l/by-id/<カメラ>-video-index0
```

画面は `http://<監視側のJetsonのIP>:8082/` で開きます。

## テスト

```bash
uv run --no-project --python 3.12 --with-requirements requirements.txt --with pytest python -m pytest -q
```

## 注意

- 判定のためのプロトタイプです。工場設備の安全装置や非常停止の代わりにはなりません
- 推論サーバーと監視画面には認証がありません。信頼できる LAN の中だけで使ってください
- カメラや VLM が失敗している間は、詰まりでベルトを止めません
