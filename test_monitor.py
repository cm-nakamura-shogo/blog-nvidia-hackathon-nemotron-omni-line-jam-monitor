import io
import json
import pytest
from PIL import Image
from monitor import prepare_image
from observation_pipeline import observe, validate_observation


def test_rotation_and_size():
    image = Image.new('RGB', (1920, 1200), 'blue')
    image.paste('red', (0, 0, 960, 600))
    raw = io.BytesIO()
    image.save(raw, format='PNG')
    converted = Image.open(io.BytesIO(prepare_image(raw.getvalue())))
    assert converted.size == (640, 400)
    r, g, b = converted.getpixel((600, 360))
    assert r > 200 and b < 30
    assert converted.getpixel((20, 20))[2] > 200


OBS = dict(operator_gesture='none')


def test_observation_validation():
    assert validate_observation(json.dumps(OBS)) == OBS
    for value in ({'operator_gesture': 'moving'}, {}, {'operator_gesture': True}):
        with pytest.raises(ValueError):
            validate_observation(json.dumps(value))


def test_observe_only_end_to_end(tmp_path):
    import subprocess
    import sys
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    buf = io.BytesIO()
    Image.new('RGB', (80, 60), 'green').save(buf, format='JPEG')
    paths = []
    result = dict(status='normal', event='none', location='foreground', reason='異常なし', evidence='画像列')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            paths.append(self.path)
            self.send_response(200)
            self.send_header('X-Captured-At', str(time.time()))
            self.end_headers()
            self.wfile.write(buf.getvalue())
        def do_POST(self):
            paths.append(self.path)
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            props = payload['response_format']['json_schema']['schema']['properties']
            output = OBS if 'operator_gesture' in props else {'blockage':'clear','reason':'none'}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({'choices':[{'finish_reason':'stop',
                'message':{'content':json.dumps(output)}}]}).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    try:
        run = subprocess.run([sys.executable, str(Path(__file__).with_name('monitor.py')),
            '--snapshot-url', url+'/snapshot.jpg', '--vlm-url', url+'/v1/chat/completions',
            '--interval', '0.5', '--jam-seconds', '1', '--out', str(tmp_path), '--once'],
            capture_output=True, text=True, timeout=15)
        assert run.returncode == 0, run.stdout + run.stderr
        saved = json.loads(next(tmp_path.glob('*/observations.jsonl')).read_text())
        # 警報がない間は障害物だけを聞き、👍は観察しない
        assert saved['observation']['operator_gesture'] == 'skipped'
        assert saved['observation']['detail_blockage']['blockage'] == 'clear'
        assert not (tmp_path/'alert_state.json').exists()
        assert set(paths) == {'/snapshot.jpg', '/v1/chat/completions'}
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize('finish,content', [('length', '{}'), ('stop', None)])
def test_incomplete_model_response_rejected(monkeypatch, finish, content):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self):
            return json.dumps({'choices': [{'finish_reason': finish,
                                           'message': {'content': content}}]}).encode()
    monkeypatch.setattr('urllib.request.urlopen', lambda *args, **kwargs: Response())
    with pytest.raises(ValueError, match='未完了') as error:
        observe('http://localhost/v1/chat/completions', 'line-base', b'jpeg')
    assert error.value.diagnostics['finish_reason'] == finish
    assert error.value.diagnostics['message']['content'] == content


def test_observation_uses_strict_schema(monkeypatch):
    captured = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self):
            return json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(OBS)}}]}).encode()
    def urlopen(request, timeout):
        captured.update(json.loads(request.data))
        return Response()
    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    observe('http://localhost/v1/chat/completions', 'line-base', b'jpeg')
    fmt = captured['response_format']
    assert fmt['type'] == 'json_schema'
    assert fmt['json_schema']['strict'] is True
    properties = fmt['json_schema']['schema']['properties']
    assert 'human_hand' not in properties and 'route_counts' not in properties
    assert set(properties) == {'operator_gesture'}


def test_jam_seconds_is_checked_only_when_several_answers_must_line_up(tmp_path):
    import subprocess, sys
    from pathlib import Path
    monitor = str(Path(__file__).with_name('monitor.py'))
    def run(*extra):
        return subprocess.run([sys.executable, monitor, '--interval', '0.5', '--out', str(tmp_path / 'runs'),
                               '--snapshot-url', 'http://127.0.0.1:9/none', '--ui-port', '0', '--once', *extra],
                              capture_output=True, text=True, timeout=30)
    assert 'jam-seconds' not in run().stderr, 'the default jam-seconds (4) must not block --jam-count 1'
    bad = run('--jam-count', '2')
    assert bad.returncode == 2 and 'jam-seconds' in bad.stderr
