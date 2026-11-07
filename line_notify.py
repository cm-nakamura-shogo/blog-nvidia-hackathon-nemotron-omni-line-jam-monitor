"""レーンの停止と再開をMQTTに出す（形はdocs/interfaces/events.md）。出すだけで、設備は動かさない。"""
import json
import uuid
from datetime import datetime, timezone

TOPIC = 'minifab/venue/device/conveyor-1/event'


class LineNotifier:
    """止めたときに conveyor.stopped、👍で再開したときに conveyor.started を出す（ダッシュボードの緊急停止）。"""

    def __init__(self, host, client=None):
        """hostのブローカーに裏でつなぐ（つながるのを待たない）。clientを渡すと、それを使う（テスト用）。"""
        if client is None:
            import paho.mqtt.client as mqtt
            # ブローカーには認証がないので、ほかのサービスと重ならない client ID にする
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f'line-monitor-{uuid.uuid4().hex[:8]}')
            client.connect_async(host)
            client.loop_start()
        self.client = client

    def _publish(self, type_, data):
        body = {'event_id': str(uuid.uuid4()), 'ts': datetime.now(timezone.utc).isoformat(), 'type': type_,
                'source': 'real', 'device_id': 'conveyor-1', 'data': data}
        try:
            self.client.publish(TOPIC, json.dumps(body), qos=1)
        except Exception as exc:
            print(f'MQTTへ{type_}を送れませんでした: {exc}', flush=True)  # 送れなくても監視と停止は続ける

    def stopped(self, incident):
        """新しく警報が立ったときに、conveyor.stopped（reasonは警報の種類）を出す。"""
        self._publish('conveyor.stopped', {'reason': incident['result']['event']})

    def started(self):
        """監視の側で再開したときに、conveyor.startedを出す。"""
        self._publish('conveyor.started', {})
