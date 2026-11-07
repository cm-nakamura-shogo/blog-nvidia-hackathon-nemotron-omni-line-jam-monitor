"""手前のベルトの詰まり監視。--control を付けたときだけ、実機のコンベアを止めたり回したりする。"""
import argparse
import fcntl
from collections import deque
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import threading
import signal
import time
import urllib.request

from PIL import Image
from alert_state import AlertState
from conveyor_control import ConveyorControl
from line_events import detect_line_event, pending_obstruction
from line_notify import LineNotifier
from monitor_ui import MonitorUI

from observation_pipeline import OBSERVATION_PROMPT


def prepare_image(raw, rotation=180):
    """カメラのJPEGを、VLMに送る向き（rotationは0か180）と大きさ（長辺640px）のJPEGにする。"""
    with Image.open(io.BytesIO(raw)) as image:
        image = image.convert('RGB')
        if rotation == 180:
            image = image.transpose(Image.Transpose.ROTATE_180)
        image.thumbnail((640, 640))
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=90)
        return output.getvalue()


def vlm_ready(vlm_url, timeout=3):
    """VLMのサーバーが応答するかを、/v1/models で確かめる。(応答したか, 応答しない理由)を返す。"""
    models_url = vlm_url.rsplit('/chat/completions', 1)[0] + '/models'
    try:
        with urllib.request.urlopen(models_url, timeout=timeout) as response:
            response.read(65536)
        return True, None
    except Exception as exc:
        return False, f'{type(exc).__name__}: {exc}'


def iso(timestamp):
    """UNIX時刻を、UTCのISO 8601の文字列にする（ログと警報の時刻）。"""
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()



def retain_missing_observation(history, record, error):
    """推論に失敗した観察を、失敗として履歴に足す。前の有効な観察は消さない（形の崩れた答え1回で証拠を失わないため）。"""
    record.update(valid=False, observation=None, error=error)
    history.append(record)


def belt_state(control, scene_state):
    """撮影した画像に付けるベルトの状態（on / off / unknown）を返す。

    --controlのときは、conveyor-mcpの最後の指令応答を読む。エージェント側の指令も反映されるので、
    自分の指令履歴よりずれない。読めなければunknownとし、詰まりと判定させない。
    --controlなしでは、--scene-stateで申告した試験の状態を返す。
    """
    if not control:
        return 'on' if scene_state == 'foreground_running_test' else scene_state
    try:
        running = control.running()
    except Exception:
        return 'unknown'
    return {True: 'on', False: 'off'}.get(running, 'unknown')


def main():
    """引数を確かめ、起動待ちの後、撮影・推論・コンベアの停止と再開・画面を回し続ける。Ctrl+CかSIGTERMで終わる。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot-url', default='http://127.0.0.1:8081/snapshot.jpg')
    parser.add_argument('--vlm-url', default='http://localhost:8001/v1/chat/completions')
    parser.add_argument('--model', default='line-base')
    parser.add_argument('--interval', type=float, default=2)
    parser.add_argument('--jam-seconds', type=float, default=4, help='--jam-countが2以上のとき、連続した観察が続く秒数')
    parser.add_argument('--jam-count', type=int, default=1, help='詰まりと確定するduck_blockedの連続回数。1なら1回で確定')
    parser.add_argument('--reason-lang', choices=['en', 'ja'], default='ja', help='VLMが返す障害物の理由の言語（判定の値は変わらない）')
    parser.add_argument('--resume-grace', type=float, default=5, help='再開してからこの秒数の間に撮った画像では、詰まりと判定しない（手をどかす動きを詰まりと取り違えないため）')
    parser.add_argument('--thumbs-count', type=int, default=1, help='警報を解除する👍の回数（先に👍なしを1回見てから数える）')
    parser.add_argument('--out', type=Path, default=Path.home() / 'line-monitor' / 'runs')
    parser.add_argument('--scene-state', choices=['unknown', 'stopped_test', 'foreground_running_test'], default='unknown')
    parser.add_argument('--control', action='store_true', help='異常時OFF・👍で再始動する実機モード（起動・終了ではコンベアに触らない）')
    parser.add_argument('--mcp-url', default='http://127.0.0.1:8000/mcp')
    parser.add_argument('--token-file', type=Path, default=Path.home() / 'conveyor-mcp' / '.token')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--mqtt-host', help='停止と👍の再開をMQTTに出すブローカー（例 192.0.2.10）。--controlのときだけ出す')
    parser.add_argument('--detail-roi', type=float, nargs=4, metavar=('LEFT', 'TOP', 'RIGHT', 'BOTTOM'),
                        help='詰まり観察用の任意の切り出し範囲（省略時は全景）（回転後の画像に対する0〜1の座標）')
    parser.add_argument('--rotation', type=int, choices=[0, 180], default=180, help='VLMに送る前に画像を回す角度')
    parser.add_argument('--ui-host', default='0.0.0.0', help='画面を待ち受けるアドレス')
    parser.add_argument('--ui-port', type=int, default=8082, help='画面のポート。0なら画面を出さない')
    parser.add_argument('--ready-interval', type=float, default=2, help='起動時に、カメラ・VLM・conveyor-mcpの応答を確かめる間隔（秒）')
    args = parser.parse_args()
    if args.detail_roi and not (0 <= args.detail_roi[0] < args.detail_roi[2] <= 1 and
                               0 <= args.detail_roi[1] < args.detail_roi[3] <= 1):
        parser.error('detail-roiは0〜1のleft top right bottomで指定してください')
    if not 0.5 <= args.interval <= 30:
        parser.error('intervalは0.5〜30秒')
    if not 1 <= args.jam_count <= 7:
        parser.error('jam-countは1〜7')
    if not 0 <= args.resume_grace <= 60:
        parser.error('resume-graceは0〜60秒')
    if not 1 <= args.thumbs_count <= 5:
        parser.error('thumbs-countは1〜5')
    # jam-secondsはjam-countが2以上のときだけ使う。既定の4秒が、使わないのに起動を止めないようにする
    if args.jam_count > 1 and not 0 < args.jam_seconds <= 7 * args.interval:
        parser.error('jam-countが2以上のとき、jam-secondsは0より大きくintervalの7倍以下')
    if args.control and args.scene_state != 'unknown':
        parser.error('--controlではscene-stateを指定せず、実際の指令履歴を使ってください')
    args.out.mkdir(parents=True, exist_ok=True)
    monitor_lock = (args.out / '.monitor.lock').open('a')
    try:
        fcntl.flock(monitor_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error('同じ保存先で監視が既に起動しています。先に終了してください')
    try:
        ui = MonitorUI(args.out / 'monitor_settings.json', args.detail_roi, args.rotation)
    except ValueError as exc:
        parser.error(f'保存した画面の設定が不正です（{args.out / "monitor_settings.json"}）: {exc}')
    if ui.loaded:
        print(f'画面で保存した設定を使います（ROI={ui.roi}、向き={ui.rotation}度）。引数の--detail-roiと--rotationより優先', flush=True)
    control = ConveyorControl(args.mcp_url, args.token_file.read_text()) if args.control else None
    run = args.out / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    run.mkdir(parents=True)
    (run / 'observation_prompt.txt').write_text(OBSERVATION_PROMPT)
    (run / 'config.json').write_text(json.dumps({
        'model': args.model, 'interval': args.interval, 'jam_seconds': args.jam_seconds, 'jam_count': args.jam_count,
        'thumbs_count': args.thumbs_count, 'resume_grace': args.resume_grace, 'exclusive_observation': True, 'reason_lang': args.reason_lang,
        'pipeline': 'jam-only-v12', 'detail_roi': ui.roi, 'scene_state': args.scene_state, 'image_max_side': 640, 'rotation': ui.rotation, 'observe_only': not args.control}, indent=2))
    notifier = LineNotifier(args.mqtt_host) if args.control and args.mqtt_host else None
    alarm = AlertState(args.out / 'alert_state.json', max_gap=max(6, args.interval * 3), thumbs_required=args.thumbs_count,
                       on_latch=notifier.stopped if notifier else None)
    alarm.retire_removed_events()
    if alarm.waiting_start:
        alarm.acknowledge()  # 以前の版の「起動時の👍待ち」は本当の警報ではないので、解除して移行する
    if alarm.waiting_start:
        print('[開始待ち] 👍を2回連続で確認するまで動かしません。', flush=True)
    elif alarm.incident:
        print('前回の警報を保持中。手を外してから👍で解除してください。', flush=True)
    condition = threading.Condition()
    stop = threading.Event()
    state = {'latest': None, 'error': None, 'generation': 0}
    # 起動待ちの間は画像を保存しない。VLMが長く落ちていると、使わない画像がたまり続けるため
    saving = threading.Event()

    def capture():
        """撮影スレッド。interval秒ごとにカメラの最新画像を取り、VLMに送る形にしてstateに置く。

        推論とは別のスレッドにして、推論が遅くても古い画像を積み上げないようにする。取れなければstateにエラーを置く。
        """
        last_time = 0
        count = 0
        last_belt = None
        while not stop.is_set():
            started = time.monotonic()
            try:
                with urllib.request.urlopen(args.snapshot_url, timeout=5) as response:
                    captured_at = float(response.headers['X-Captured-At'])
                    raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise ValueError('画像サイズ上限超過')
                age = time.time() - captured_at
                if not 0 <= age <= 3 or captured_at <= last_time:
                    raise ValueError('画像が古いか、同じフレームです')
                jpeg = prepare_image(raw, ui.rotation)
                ui.set_frame(jpeg)
                path = run / f'frame_{count:06d}.jpg'
                if saving.is_set():
                    path.write_bytes(jpeg)
                # 撮影の直後に読み、この画像を撮ったときの状態にする（推論の後に読むと数秒ずれる）
                belt = belt_state(control, args.scene_state)
                if belt != last_belt:
                    print(f'ベルト（MCPの最終応答）: {belt}', flush=True)
                    last_belt = belt
                frame = {'time': captured_at, 'jpeg': jpeg, 'file': path.name, 'foreground_command': belt}
                with condition:
                    state['latest'] = frame
                    state['error'] = None
                    condition.notify_all()
                last_time = captured_at
                count += 1
            except Exception as exc:
                with condition:
                    state['latest'] = None
                    state['generation'] += 1
                    state['error'] = f'{type(exc).__name__}: {exc}'
                    condition.notify_all()
            stop.wait(max(0.01, args.interval - (time.monotonic() - started)))

    worker = threading.Thread(target=capture, daemon=True)
    worker.start()
    print(f"{'実機制御あり：起動ではコンベアに触らず、異常時に停止' if control else '判定のみ・停止操作なし'}。保存先: {run}", flush=True)
    # 起動ではコンベアに触らない。保存された警報は、前回の監視が止めたものとして扱う
    stop_confirmed = alarm.incident is not None
    stopped_at = 0.0  # 監視がOFFを送る前に撮った画像には、古いONが付いている。それで警報を解かないための時刻
    def interrupt(signum, frame):
        """SIGTERM（systemctl stop など）をCtrl+Cと同じに扱い、後片付けを通す。"""
        raise KeyboardInterrupt
    previous_term = signal.signal(signal.SIGTERM, interrupt)
    last_inferred = None
    last_error = None
    history = deque(maxlen=8)
    blockage_history = deque(maxlen=64)
    control_epoch = 0.0
    resumed_at = 0.0  # 再開した時刻。監視エラーで更新するcontrol_epochとは分ける
    history_generation = 0
    from parallel_observation import ParallelObserver
    parallel_observer = ParallelObserver(args.vlm_url, args.model, ui.roi, args.reason_lang)
    if args.ui_port and ui.serve(args.ui_host, args.ui_port):
        print(f'画面: http://{args.ui_host}:{args.ui_port}/', flush=True)
    try:
        with (run / 'judgments.jsonl').open('a', buffering=1) as log, \
                (run / 'observations.jsonl').open('a', buffering=1) as observation_log, \
                (run / 'blockage_observations.jsonl').open('a', buffering=1) as blockage_log:
            def belt_now():
                """画面に出すベルトの状態。最新の画像に付けたconveyor-mcpの状態を返す。"""
                with condition:
                    latest = state['latest']
                return latest['foreground_command'] if latest else 'unknown'

            def handle_ui():
                """画面から積まれた操作（停止、再開、ROIと向きの変更）を、この制御スレッドで実行する。"""
                nonlocal stop_confirmed, stopped_at, control_epoch, resumed_at
                for command in ui.take_commands():
                    action = command['action']
                    try:
                        if action == 'settings':
                            ui.apply_settings(command['detail_roi'], command['rotation'])
                            parallel_observer.roi = ui.roi
                            blockage_history.clear()  # 範囲や向きが変わった前後の観察は、続けて数えない
                            history.clear()
                            message = f'ROI={ui.roi} 向き={ui.rotation}度'
                        elif not control:
                            raise RuntimeError('--controlなしではコンベアを操作できません')
                        elif action == 'stop':
                            try:
                                control.stop()
                            except Exception:
                                stop_confirmed = False
                                print('停止応答を確認できません。物理スイッチで停止してください。', flush=True)
                                raise
                            stop_confirmed = True
                            stopped_at = time.time()
                            # OFFの応答を確かめてから警報にする。conveyor.stopped（reason: manual）が、本当に止めたときだけ出るように
                            alarm.latch({'status': 'abnormal', 'event': 'manual', 'reason': '画面から停止'}, iso(time.time()))
                            message = 'MCPのOFF応答を確認'
                        else:
                            control.start()
                            stop_confirmed = False
                            alarm.acknowledge()
                            control_epoch = resumed_at = time.time()
                            blockage_history.clear()
                            history.clear()
                            if notifier:
                                notifier.started()
                            message = '警報を解除し、MCPのON応答を確認'
                        ui.finish(command, True, message)
                        log.write(json.dumps({'time': iso(time.time()), 'transition': 'screen_' + action, 'message': message,
                                              'incident': alarm.incident, 'observe_only': not args.control}, ensure_ascii=False) + '\n')
                        print(f'[画面] {action}: {message}', flush=True)
                    except Exception as exc:
                        ui.finish(command, False, f'{type(exc).__name__}: {exc}')
                        print(f'[画面] {action}に失敗: {type(exc).__name__}: {exc}', flush=True)
                ui.update(belt=belt_now(), incident=alarm.incident, gesture_count=alarm.count, gesture_required=alarm.thumbs_required)

            def dependencies():
                """カメラ・VLM・conveyor-mcpが応答するかを確かめる。{名前: {'ok': bool, 'error': 理由}}を返す。"""
                with condition:
                    camera = {'ok': state['latest'] is not None and not state['error'], 'error': state['error']}
                ok, error = vlm_ready(args.vlm_url)
                checks = {'camera': camera, 'vlm': {'ok': ok, 'error': error}}
                if control:
                    try:
                        control.running()
                        checks['mcp'] = {'ok': True, 'error': None}
                    except Exception as exc:
                        checks['mcp'] = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
                return checks

            # 自動起動では、カメラ・VLM・conveyor-mcpがまだ立ち上がっていないことがある。
            # 推論の失敗を監視の異常として警報にしないよう、3つが応答するまで推論を始めない。
            # 待っている間も画面の停止・再開は実行する（監視の準備ができていなくても、コンベアを止められるように）
            last_waiting = None
            while not args.once:  # --onceは1回だけ試す指定なので待たない
                checks = dependencies()
                waiting = [name for name, check in checks.items() if not check['ok']]
                ui.update(phase='waiting' if waiting else 'monitoring', dependencies=checks)
                if not waiting:
                    break
                if waiting != last_waiting:
                    print('[起動待ち] 応答を待っています: ' + '、'.join(waiting), flush=True)
                    last_waiting = waiting
                handle_ui()
                stop.wait(args.ready_interval)
            saving.set()
            print('[起動] カメラ・VLM・conveyor-mcpの応答を確認。監視を始めます', flush=True)

            while True:
                handle_ui()
                with condition:
                    condition.wait_for(lambda: state['error'] != last_error or
                        (state['latest'] is not None and state['latest']['time'] != last_inferred), timeout=1)
                    if state['generation'] != history_generation:
                        alarm.invalidate()
                        history.clear()
                        history_generation = state['generation']
                        blockage_history.clear()
                    error = state['error']
                    if error:
                        # 処理できないときは判定不能として記録するだけで、ベルトにも警報にも触らない
                        if error != last_error:
                            row = {'time': iso(time.time()), 'status': 'unknown', 'capture_error': error,
                                   'observe_only': not args.control}
                            log.write(json.dumps(row, ensure_ascii=False) + '\n')
                            print('判定不能: ' + error, flush=True)
                        last_error = error
                        if args.once:
                            return 1
                        continue
                    last_error = None
                    if state['latest'] is None or state['latest']['time'] == last_inferred:
                        continue
                    frame = state['latest']
                    generation = state['generation']
                    last_inferred = frame['time']
                started = time.monotonic()
                record = {'captured_at': iso(frame['time']), 'time': frame['time'], 'file': frame['file']}
                row = {'time': iso(time.time()), 'observe_only': not args.control}
                phase = 'observation'
                try:
                    record['foreground_command'] = frame['foreground_command']
                    if (control and alarm.incident and stop_confirmed and frame['foreground_command'] == 'on'
                            and frame['time'] > stopped_at):
                        # 監視がOFFを確かめた後に、別の経路（エージェント側など）でONになった。人が再開したものとして警報を解く。
                        # OFFに失敗したときはstop_confirmedがFalseのままなので、止まっていないベルトで警報は解けない
                        alarm.acknowledge()
                        stop_confirmed = False
                        control_epoch = resumed_at = time.time()
                        blockage_history.clear()
                        history.clear()
                        if notifier:
                            notifier.started()
                        log.write(json.dumps({'time': iso(time.time()), 'status': 'normal', 'transition': 'external_restart',
                                              'captured_at': record['captured_at'], 'observe_only': False}, ensure_ascii=False) + '\n')
                        print('[通常監視] ほかの経路でベルトがONになったため、人が再開したものとして警報を解除しました。', flush=True)
                    phase = 'parallel_observation'
                    def latest_for_blockage():
                        """障害物を見る最新の画像。カメラの異常中と、再開より前に撮った画像はNoneにする。"""
                        with condition:
                            latest = state['latest']
                            if (state['error'] or latest is None or latest['time'] <= control_epoch
                                    or latest['time'] <= resumed_at + args.resume_grace):
                                return None  # 再開の直後は、手をどかす動きを詰まりと取り違えやすいので見ない
                            return {**latest, 'generation': state['generation']}
                    def on_blockage(blockage, source, raw_block, usage_block, elapsed):
                        """障害物の答えが1件返るたびに呼ばれる。詰まりが確定したら、👍の観察を待たずにその場で止める。"""
                        nonlocal stop_confirmed, stopped_at
                        with condition:
                            invalid = state['error'] or state['generation'] != source['generation']
                        age = time.time() - source['time']
                        if invalid or age > max(10, args.interval * 3):
                            raise ValueError('障害物応答時に画像が古い、またはカメラ接続異常')
                        if source['time'] <= control_epoch or source['time'] <= resumed_at + args.resume_grace:
                            return  # 再開より前か、再開の直後に撮った画像の答えで、再開したベルトを止めない
                        partial = {'captured_at': iso(source['time']), 'time': source['time'],
                            'file': source['file'], 'foreground_command': source['foreground_command'],
                            'valid': True, 'observation': {'detail_blockage': blockage},
                            'raw_response': raw_block, 'usage': usage_block,
                            'inference_seconds': elapsed, 'frame_age_seconds': age}
                        blockage_history.append(partial)
                        blockage_log.write(json.dumps(partial, ensure_ascii=False) + '\n')
                        ui.update(blockage=blockage, inference_seconds=elapsed)
                        ui.add_result(kind='blockage', captured_at=partial['captured_at'],
                                      result=blockage['blockage'], reason=blockage['reason'], seconds=elapsed)
                        print(f"障害物監視 {source['file']}: {blockage['blockage']} "
                              f"推論={elapsed:.2f}秒 画像経過={age:.2f}秒", flush=True)
                        event = detect_line_event(blockage_history, args.jam_seconds, max(6, args.interval * 3), args.jam_count)
                        if event and event['event'] == 'jam':
                            alarm.latch(event, partial['captured_at'])
                            if control and not stop_confirmed:
                                control.stop()
                                stop_confirmed = True
                                stopped_at = time.time()
                                print('[停止] 障害物の応答でOFF確認（他の推論は待ちません）', flush=True)
                            log.write(json.dumps({'time': iso(time.time()), 'phase': 'early_blockage',
                                'file': source['file'], 'decision': event, 'observe_only': not args.control,
                                'stop_acknowledged': bool(control and stop_confirmed),
                                'frame_age_seconds': time.time() - source['time']}, ensure_ascii=False) + '\n')
                    # VLMへの要求は1本ずつ。警報がない間は障害物だけ、警報中は👍だけ（2本同時だとAGX Orinでは遅い）
                    held = alarm.incident is not None
                    observation, metadata, detail_jpeg = parallel_observer.run_independent(
                        frame, latest_for_blockage, on_blockage, handle_ui, gesture=held, blockage=not held)
                    record.update(metadata)
                    (run / ('detail_' + frame['file'])).write_bytes(detail_jpeg)
                    record['observation'] = observation
                    age = time.time() - frame['time']
                    with condition:
                        camera_invalid = state['error'] or state['generation'] != generation
                    if age > max(10, args.interval * 3) or camera_invalid:
                        raise ValueError('観察完了時に画像が古い、またはカメラ接続異常')
                    record['valid'] = True
                    observation_log.write(json.dumps(record, ensure_ascii=False) + '\n')
                    # 間が大きく空いたら続きとして扱わない。カメラや推論が止まっていた間をまたいで判断しないため
                    if history and frame['time'] - history[-1]['time'] > max(6, args.interval * 3):
                        history.clear()
                    while history and frame['time'] - history[0]['time'] > max(20, args.interval * 8):
                        history.popleft()
                    history.append(record)
                    if observation['operator_gesture'] != 'skipped':
                        print(f"観察 {frame['file']}: サイン={observation['operator_gesture']}", flush=True)
                        ui.update(gesture=observation['operator_gesture'])
                        ui.add_result(kind='gesture', captured_at=record['captured_at'], result=observation['operator_gesture'],
                                      reason=None, seconds=(metadata.get('inference_timings', {}).get('full') or {}).get('finished_seconds'))
                    if alarm.observe_gesture(observation, frame['time']):
                        if control:
                            if not stop_confirmed:
                                raise RuntimeError('停止応答未確認のため再始動を拒否')
                            control.start()
                            stop_confirmed = False
                            if notifier:
                                notifier.started()
                        alarm.acknowledge()
                        control_epoch = resumed_at = time.time()
                        blockage_history.clear()
                        history.clear()
                        history.append(record)
                        row.update(status='normal', transition='operator_acknowledged',
                                   acknowledged_by=frame['file'], captured_at=record['captured_at'],
                                   incident=None, equipment_restarted=bool(control))
                        log.write(json.dumps(row, ensure_ascii=False) + '\n')
                        print('[通常監視] 👍を2回確認。警報解除・履歴更新。' + ('MCPのON応答を確認、再始動しました。' if control else '判定のみ、再始動しません。'), flush=True)
                        if args.once:
                            return 0
                        continue
                    if alarm.incident:
                        print(f"[{'開始待ち' if alarm.waiting_start else '警報保持'}] {alarm.incident['result']['event']} "
                              f"解除サイン={alarm.count}/{args.thumbs_count}", flush=True)
                    if args.once:
                        return 0
                    if sum(bool(r.get('observation')) for r in history) < 3:
                        print('比較用の観察を収集中（3件から総合判定）', flush=True)
                        continue
                    row['observations'] = [
                        {'file': r['file'], 'captured_at': r['captured_at'], 'observation': r.get('observation'),
                         'foreground_command': r.get('foreground_command'), 'missing_reason': r.get('error')}
                        for r in history]
                    phase = 'assessment'
                    result = detect_line_event(blockage_history, args.jam_seconds, max(6, args.interval * 3), args.jam_count) or pending_obstruction(blockage_history) or {
                        'status': 'normal', 'event': 'none', 'location': 'foreground',
                        'reason': '詰まりの確認条件に達する観察はありません',
                        'evidence': '独立した障害物観察と時刻を比較'}
                    row.update(decision=result)
                    ui.update(decision={k: result.get(k) for k in ('status', 'event', 'reason')})
                    age = time.time() - frame['time']
                    row.update(newest_frame_age_seconds=round(age, 2))
                    with condition:
                        camera_invalid = state['error'] or state['generation'] != generation
                    if age > max(10, args.interval * 3) or camera_invalid:
                        row.update(status='unknown', error='応答時に画像が古い、またはカメラ接続異常')
                        alarm.invalidate()
                        history.clear()
                    else:
                        row['status'] = result['status']
                    if 'error' not in row:
                        alarm.latch(result, record['captured_at'])
                    if control and alarm.incident and not stop_confirmed:
                        control.stop()
                        stop_confirmed = True
                        stopped_at = time.time()
                        print('[停止] MCPのOFF応答を確認', flush=True)
                    print(f"[今回の判定:{row['status']}] {result['event']}: {result['reason']}"
                          + ('（判定無効）' if 'error' in row else ''), flush=True)
                except Exception as exc:
                    row['error_phase'] = phase
                    if hasattr(exc, 'diagnostics'):
                        row['model_error'] = exc.diagnostics
                        if phase == 'observation':
                            record['model_error'] = exc.diagnostics
                    with condition:
                        camera_invalid = state['error'] or state['generation'] != generation
                    stale = time.time() - frame['time'] > max(10, args.interval * 3)
                    if not record.get('valid'):
                        if not camera_invalid and not stale:
                            retain_missing_observation(history, record, f'{type(exc).__name__}: {exc}')
                        else:
                            record.update(valid=False, observation=None, error=f'{type(exc).__name__}: {exc}')
                        observation_log.write(json.dumps(record, ensure_ascii=False) + '\n')
                    # 答えの形が崩れただけなら、前の有効な観察は消さない。
                    # カメラが止まった、画像が古いときは、続きとして扱えないので消す
                    # 処理できなかった観察では、ベルトにも警報にも触らない（判定不能として記録するだけ）
                    alarm.invalidate()
                    blockage_history.clear()
                    control_epoch = time.time()
                    if camera_invalid or stale:
                        history.clear()
                    row.update(status='unknown', error=f'{type(exc).__name__}: {exc}')
                    print('判定不能: ' + row['error'], flush=True)
                    stop.wait(0.1)
                row['assessment_status'] = row['status']
                row['incident'] = alarm.incident
                if alarm.incident:
                    row['status'] = 'waiting_start' if alarm.waiting_start else 'abnormal'
                    print('[開始待ち] 👍を確認するまで待機。' if alarm.waiting_start else '[警報保持] 👍で解除するまで継続します。', flush=True)
                row['elapsed_seconds'] = round(time.monotonic() - started, 3)
                log.write(json.dumps(row, ensure_ascii=False) + '\n')
                ui.update(belt=belt_now(), incident=alarm.incident, gesture_count=alarm.count, gesture_required=alarm.thumbs_required,
                          error=row.get('error'))
                if args.once:
                    return 0 if row['status'] != 'unknown' else 1
    except KeyboardInterrupt:
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        if control:
            print('[終了] コンベアには触りません。監視なしで回り続けることがあります', flush=True)
        ui.close()
        stop.set()
        worker.join(timeout=6)
        if parallel_observer:
            print('[終了] 実行中の推論が終わるのを待っています（数秒）', flush=True)
            parallel_observer.close()


if __name__ == '__main__':
    raise SystemExit(main())
