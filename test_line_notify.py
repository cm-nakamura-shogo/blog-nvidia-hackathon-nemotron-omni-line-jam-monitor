import json

from alert_state import AlertState
from line_notify import LineNotifier

BAD = {'status': 'abnormal', 'event': 'jam'}


class FakeClient:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, qos):
        self.sent.append((topic, json.loads(payload)))


def test_stopped_and_started_follow_events_md():
    client = FakeClient()
    notifier = LineNotifier('unused', client=client)
    notifier.stopped({'result': BAD})
    notifier.started()
    (topic, stopped), (_, started) = client.sent
    assert topic == 'minifab/venue/device/conveyor-1/event'
    assert stopped['type'] == 'conveyor.stopped' and stopped['source'] == 'real' and stopped['data'] == {'reason': 'jam'}
    assert started['type'] == 'conveyor.started'


def test_latch_notifies_only_new_abnormal_incident():
    seen = []
    state = AlertState(on_latch=seen.append)
    state.latch(BAD, 't1')
    state.latch(BAD, 't2')  # 警報保持中は新しい停止ではない
    assert [i['detected_at'] for i in seen] == ['t1']
