"""V4L2のカメラのMJPEGを配る（標準ライブラリだけ）。コンベアには触らない。"""
import argparse
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# 映像だけを出す。ROIと向きは、動いている監視の値を変えるために監視の画面（monitor_ui.py、8082番）で調整する
PAGE = '''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>カメラ</title><style>body{margin:0;background:#000;color:#ccc;font:14px system-ui,sans-serif}img{display:block;width:100%}p{margin:8px 16px}</style></head>
<body><img src="/stream" alt="カメラの映像"><p>カメラの生の映像です（回していません）。ROIと向きは、詰まり監視の画面（8082番）で調整します。</p></body></html>
'''


def main():
    """v4l2-ctlでカメラを開き、最新の1枚（/snapshot.jpg）と動画（/stream）をHTTPで配る。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--camera', required=True)
    parser.add_argument('--port', type=int, default=8081)
    parser.add_argument('--host', default='127.0.0.1', help='待ち受けるアドレス。ダッシュボードなどLANから読む場合は 0.0.0.0（同じ機械の監視は 127.0.0.1 のまま読める）')
    args = parser.parse_args()
    condition = threading.Condition()
    state = {'frame': None, 'sequence': 0, 'ended': False, 'captured_at': 0.0, 'monotonic': 0.0}
    process = subprocess.Popen([
        'v4l2-ctl', '-d', args.camera,
        '--set-fmt-video=width=1920,height=1200,pixelformat=MJPG',
        '--set-parm=10', '--stream-mmap', '--stream-to=-', '--stream-count=0',
    ], stdout=subprocess.PIPE)

    def capture():
        """v4l2-ctlの出力からJPEGを切り出し、最新の1枚と撮った時刻をstateに置く。"""
        buffer = bytearray()
        try:
            while True:
                chunk = process.stdout.read1(65536)
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    start = buffer.find(b'\xff\xd8')
                    if start < 0:
                        buffer[:] = buffer[-1:]
                        break
                    if start:
                        del buffer[:start]
                    end = buffer.find(b'\xff\xd9', 2)
                    if end < 0:
                        break
                    frame = bytes(buffer[:end + 2])
                    del buffer[:end + 2]
                    with condition:
                        state['frame'] = frame
                        state['captured_at'] = time.time()
                        state['monotonic'] = time.monotonic()
                        state['sequence'] += 1
                        condition.notify_all()
                if len(buffer) > 16 * 1024 * 1024:
                    raise RuntimeError('Camera stream contains no complete JPEG')
        finally:
            with condition:
                state['ended'] = True
                condition.notify_all()

    class Handler(BaseHTTPRequestHandler):
        """映像だけを返す。設定を変える口は持たない。"""

        def do_GET(self):
            """/ は映像のページ、/snapshot.jpg は撮った時刻付きの最新の1枚（2秒より古ければ503）、/stream は動画。"""
            if self.path == '/':
                body = PAGE.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == '/snapshot.jpg':
                with condition:
                    frame = state['frame']
                    captured_at = state['captured_at']
                    sequence = state['sequence']
                    stale = state['ended'] or time.monotonic() - state['monotonic'] > 2
                if frame is None or stale:
                    self.send_error(503, 'No fresh camera frame')
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', str(len(frame)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Captured-At', str(captured_at))
                self.send_header('X-Frame-Sequence', str(sequence))
                self.end_headers()
                self.wfile.write(frame)
                return
            if self.path != '/stream':
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            sequence = 0
            try:
                while True:
                    with condition:
                        ready = condition.wait_for(
                            lambda: state['sequence'] != sequence or state['ended'], timeout=10)
                        if not ready or state['ended']:
                            return
                        frame, sequence = state['frame'], state['sequence']
                    self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '
                                     + str(len(frame)).encode() + b'\r\n\r\n' + frame + b'\r\n')
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = None
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        threading.Thread(target=capture, daemon=True).start()
        print(f'Preview: http://{args.host}:{args.port} (Ctrl+C to stop)', flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()


if __name__ == '__main__':
    main()
