import json
import pytest
from conveyor_control import ConveyorControl


def test_mcp_requires_ack_and_never_infers_success_from_http(monkeypatch):
    captured = []
    state = {'simulation': False, 'closed': False, 'fault': None, 'last_acknowledged_running': False}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps({'result': {'content': [{'type': 'text', 'text': json.dumps(state)}]}}).encode()
    def urlopen(req, timeout):
        captured.append(json.loads(req.data))
        return Response()
    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    control = ConveyorControl('http://localhost:8000/mcp', 'test-token')
    control.stop()
    with pytest.raises(RuntimeError): control.start()
    state['last_acknowledged_running'] = True
    control.start()
    state['simulation'] = True
    with pytest.raises(RuntimeError): control.start()
    with pytest.raises(ValueError): control.call('arbitrary_tool')
    assert {r['params']['name'] for r in captured} == {'start_conveyor', 'stop_conveyor'}


def test_status_only_reads_the_last_acknowledgement(monkeypatch):
    captured = []
    state = {'simulation': False, 'closed': False, 'fault': None, 'last_acknowledged_running': True}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps({'result': {'content': [{'type': 'text', 'text': json.dumps(state)}]}}).encode()
    def urlopen(req, timeout):
        captured.append(json.loads(req.data))
        return Response()
    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    control = ConveyorControl('http://localhost:8000/mcp', 'test-token')
    assert control.running() is True
    state['last_acknowledged_running'] = False
    assert control.running() is False
    state['last_acknowledged_running'] = None  # リレーの指令に失敗すると、状態は分からない（None）になる
    assert control.running() is None
    state['simulation'] = True
    with pytest.raises(RuntimeError): control.running()
    assert {r['params']['name'] for r in captured} == {'get_conveyor_status'}


def test_belt_state_follows_mcp_and_never_guesses():
    from monitor import belt_state
    class Control:
        def __init__(self, value): self.value = value
        def running(self):
            if isinstance(self.value, Exception): raise self.value
            return self.value
        def start(self): pytest.fail('reading the state must not command the conveyor')
        stop = start
    assert belt_state(Control(True), 'unknown') == 'on'
    assert belt_state(Control(False), 'unknown') == 'off'
    assert belt_state(Control(None), 'unknown') == 'unknown'
    assert belt_state(Control(OSError('MCP down')), 'unknown') == 'unknown'
    assert belt_state(None, 'foreground_running_test') == 'on'
    assert belt_state(None, 'stopped_test') == 'stopped_test'
