import json
import urllib.error
import urllib.request

import pytest

from monitor_ui import MonitorUI, valid_roi, valid_rotation


def test_roi_and_rotation_validation():
    assert valid_roi(None) is None
    assert valid_roi([0, 0.1, 0.5, 1]) == [0.0, 0.1, 0.5, 1.0]
    for bad in ([0.5, 0, 0.4, 1], [0, 0, 1], [0, 0, 1, 1.2], ['0', 0, 1, 1], [True, 0, 1, 1], 'x'):
        with pytest.raises(ValueError):
            valid_roi(bad)
    assert valid_rotation(0) == 0 and valid_rotation(180) == 180
    with pytest.raises(ValueError):
        valid_rotation(90)


def test_saved_settings_win_over_arguments_and_survive_restart(tmp_path):
    path = tmp_path / 'monitor_settings.json'
    ui = MonitorUI(path, [0.2, 0, 0.8, 1], 180)
    assert not ui.loaded and ui.roi == [0.2, 0, 0.8, 1] and ui.rotation == 180
    ui.apply_settings([0.5, 0.1, 0.9, 0.6], 0)
    again = MonitorUI(path, [0.2, 0, 0.8, 1], 180)
    assert again.loaded and again.roi == [0.5, 0.1, 0.9, 0.6] and again.rotation == 0
    path.write_text(json.dumps({'detail_roi': [1, 0, 0, 1], 'rotation': 0}))
    with pytest.raises(ValueError):
        MonitorUI(path, None, 180)


def request(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_http_only_queues_commands(tmp_path):
    ui = MonitorUI(tmp_path / 'settings.json', None, 180)
    server = ui.serve('127.0.0.1', 0)
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        code, body = request(base + '/')
        assert code == 200 and '詰まり監視'.encode() in body and b'id="roiButton"' in body
        assert request(base + '/debug')[0] == 404, 'one screen only'
        assert request(base + '/frame.jpg')[0] == 503
        ui.set_frame(b'jpeg')
        assert request(base + '/frame.jpg') == (200, b'jpeg')
        assert request(base + '/command', {'action': 'start_conveyor'})[0] == 400
        assert request(base + '/settings', {'detail_roi': [0.9, 0, 0.1, 1], 'rotation': 0})[0] == 400
        assert request(base + '/command', {'action': 'stop'})[0] == 202
        assert request(base + '/settings', {'detail_roi': None, 'rotation': 0})[0] == 202
        taken = ui.take_commands()
        assert [c['action'] for c in taken] == ['stop', 'settings']
        assert taken[1]['detail_roi'] is None and taken[1]['rotation'] == 0
        assert ui.rotation == 180, 'the HTTP thread must not apply settings itself'
        ui.update(belt='on')
        ui.finish(taken[0], True, 'ok')
        status = json.loads(request(base + '/status.json')[1])
        assert status['status']['belt'] == 'on' and status['last_command']['action'] == 'stop'
    finally:
        ui.close()


def test_results_are_newest_first_and_bounded(tmp_path):
    ui = MonitorUI(tmp_path / 'settings.json', None, 180)
    server = ui.serve('127.0.0.1', 0)
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        for n in range(35):
            ui.add_result(kind='blockage', captured_at=f't{n}', result='clear', reason='no duck', seconds=1.0, belt='on')
        results = json.loads(request(base + '/status.json')[1])['results']
        assert len(results) == 30 and results[0]['captured_at'] == 't34'
    finally:
        ui.close()


def test_dropped_image_transfer_is_not_an_error(tmp_path, capsys):
    """ブラウザが要らなくなった画像の取得を打ち切っても、サーバーがTracebackを出さないこと。"""
    import socket
    import struct
    import time
    ui = MonitorUI(tmp_path / 'settings.json', None, 180)
    ui.set_frame(b'x' * (8 * 1024 * 1024))  # 相手が接続を切った時点で、まだ送っている途中になる大きさ
    server = ui.serve('127.0.0.1', 0)
    try:
        for _ in range(3):
            client = socket.create_connection(('127.0.0.1', server.server_port))
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))  # 閉じるときにRSTを送り、打ち切りを再現する
            client.sendall(b'GET /frame.jpg HTTP/1.1\r\nHost: x\r\n\r\n')
            client.recv(1024)
            client.close()
        time.sleep(.5)
        assert request(f'http://127.0.0.1:{server.server_port}/status.json')[0] == 200
    finally:
        ui.close()
    assert 'Traceback' not in capsys.readouterr().err
