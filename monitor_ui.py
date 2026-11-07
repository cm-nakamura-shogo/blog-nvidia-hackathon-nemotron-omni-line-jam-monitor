"""詰まり監視の画面（8082番）。映像とROI、向き、状態、推論の結果、コンベアの停止と再開。

HTTPのスレッドは操作をキューに積むだけで、実行は監視の制御スレッドが行う（take_commands）。
画面からの操作が、MCPの指令や警報の更新と競合しないようにするため。
"""
import json
import queue
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROTATIONS = (0, 180)
ACTIONS = ('stop', 'resume')


def valid_roi(roi):
    """ROIを確かめて返す。Noneは全景。値は回したあとの画像に対する[left, top, right, bottom]（0〜1）。

    形が違えばValueErrorを出す。JSONのtrueは数値として通さない（boolはintの一種のため）。
    """
    if roi is None:
        return None
    if (not isinstance(roi, (list, tuple)) or len(roi) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in roi)):
        raise ValueError('ROIは4つの数値（left top right bottom）')
    left, top, right, bottom = (float(v) for v in roi)
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError('ROIは0〜1で、left<right、top<bottom')
    return [left, top, right, bottom]


def valid_rotation(rotation):
    """向き（0か180）を確かめて返す。それ以外はValueErrorを出す。"""
    if rotation not in ROTATIONS:
        raise ValueError('向きは0か180')
    return rotation


class MonitorUI:
    """画面のHTTPサーバーと、監視との受け渡し（状態、映像、推論の結果、操作のキュー、ROIと向きの設定）。

    監視の側は、状態をupdate・set_frame・add_resultで渡し、take_commandsで操作を受け取って実行し、finishで結果を返す。
    """

    def __init__(self, settings_path, roi, rotation):
        """settings_pathに保存した設定があれば、引数のroiとrotationより優先して読む。中身が不正ならValueErrorを出す。"""
        self.path = Path(settings_path)
        self.lock = threading.Lock()
        self.commands = queue.Queue()
        self.settings = {'detail_roi': valid_roi(roi), 'rotation': valid_rotation(rotation)}
        self.loaded = False
        if self.path.exists():
            # 画面で合わせた値を、古い引数のまま起動し直しても失わないようにする
            saved = json.loads(self.path.read_text())
            self.settings = {'detail_roi': valid_roi(saved.get('detail_roi')),
                             'rotation': valid_rotation(saved.get('rotation'))}
            self.loaded = True
        self.status = {}
        self.jpeg = None
        self.results = deque(maxlen=30)
        self.last_command = None
        self.server = None
        self.sequence = 0

    @property
    def rotation(self):
        """今の向き。撮影スレッドが画像を回すときに読む。"""
        with self.lock:
            return self.settings['rotation']

    @property
    def roi(self):
        """今のROI。Noneは全景。"""
        with self.lock:
            return self.settings['detail_roi']

    def apply_settings(self, roi, rotation):
        """制御スレッドから呼ぶ。設定を変え、次の起動でも使えるようにファイルに保存する。

        書きかけのファイルが残らないよう、一時ファイルに書いてから置き換える。
        """
        with self.lock:
            self.settings = {'detail_roi': valid_roi(roi), 'rotation': valid_rotation(rotation)}
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.settings))
            temporary.replace(self.path)

    def set_frame(self, jpeg):
        """画面に出す最新の画像（VLMに送る向きのJPEG）を渡す。"""
        with self.lock:
            self.jpeg = jpeg

    def add_result(self, **result):
        """VLMの答えを1件足す。kind（blockage / gesture）、captured_at、result、reason、seconds。新しい順に30件まで持つ。"""
        with self.lock:
            self.results.appendleft(result)

    def update(self, **status):
        """画面に出す状態（ベルト、警報、👍の回数など）を、渡した項目だけ上書きする。"""
        with self.lock:
            self.status.update(status, updated_at=time.time())

    def finish(self, command, ok, message):
        """制御スレッドが操作を実行した結果。画面は数秒だけ表示する。"""
        with self.lock:
            self.last_command = {'id': command['id'], 'action': command['action'], 'ok': ok,
                                 'message': message, 'finished_at': time.time()}

    def submit(self, action, **params):
        """操作をキューに積み、番号を返す。ここでは実行しない。"""
        with self.lock:
            self.sequence += 1
            command = {'id': self.sequence, 'action': action, **params}
        self.commands.put(command)
        return command['id']

    def take_commands(self):
        """積まれた操作をすべて取り出す。待たずに返す（制御スレッドの処理を止めないため）。"""
        taken = []
        while True:
            try:
                taken.append(self.commands.get_nowait())
            except queue.Empty:
                return taken

    def snapshot(self):
        """/status.json で返す中身。"""
        with self.lock:
            return {'status': dict(self.status), 'settings': dict(self.settings), 'last_command': self.last_command,
                    'results': list(self.results)}

    def serve(self, host, port):
        """別スレッドでHTTPサーバーを起動し、サーバーを返す。ポートが使えなければNoneを返し、監視は画面なしで続ける。"""
        ui = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, code, body, content_type):
                self.send_response(code)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # ブラウザが要らなくなった画像の取得を打ち切っただけ。エラーとして記録しない

            def send_json(self, code, value):
                self.send(code, json.dumps(value, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

            def do_GET(self):
                path = self.path.split('?', 1)[0]
                if path == '/':
                    self.send(200, PAGE.encode(), 'text/html; charset=utf-8')
                elif path == '/status.json':
                    self.send_json(200, ui.snapshot())
                elif path == '/frame.jpg':
                    with ui.lock:
                        jpeg = ui.jpeg
                    if jpeg is None:
                        self.send_error(503, 'No frame yet')
                    else:
                        self.send(200, jpeg, 'image/jpeg')
                else:
                    self.send_error(404)

            def do_POST(self):
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                    if length > 4096:
                        raise ValueError('要求が大きすぎます')
                    body = json.loads(self.rfile.read(length) or b'{}')
                    if self.path == '/command':
                        if body.get('action') not in ACTIONS:
                            raise ValueError('操作はstopかresume')
                        command_id = ui.submit(body['action'])
                    elif self.path == '/settings':
                        command_id = ui.submit('settings', detail_roi=valid_roi(body.get('detail_roi')),
                                               rotation=valid_rotation(body.get('rotation')))
                    else:
                        self.send_error(404)
                        return
                except (ValueError, TypeError) as exc:
                    self.send_json(400, {'error': str(exc)})
                    return
                self.send_json(202, {'queued': command_id})

        try:
            self.server = ThreadingHTTPServer((host, port), Handler)
        except OSError as exc:
            print(f'画面を {host}:{port} で開けません（{exc}）。画面なしで監視を続けます', flush=True)
            return None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.server

    def close(self):
        """HTTPサーバーを止める。"""
        if self.server:
            self.server.shutdown()
            self.server.server_close()


# ダッシュボードのタイルに収まる、ダークテーマの1画面。状態と推論の結果は映像の上に重ねる。
# ROIの調整は「ROI」を押したときだけ出す。タイルから誤って変えないため
PAGE = '''<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>詰まり監視</title>
<style>
:root{color-scheme:dark;--bg:#0b0d10;--ink:#f2f4f6;--muted:#a3acb5;--ok:#3ccf7a;--alarm:#ff5a4e;--warn:#f5c04a;--roi:#2fd07a}
*{box-sizing:border-box}[hidden]{display:none!important}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:clamp(11px,1.4vw,20px)/1.35 system-ui,sans-serif;overflow:hidden;display:flex;align-items:center;justify-content:center}
.stage{position:relative;line-height:0;user-select:none;touch-action:none}
.stage img{display:block;width:100vw;height:auto;-webkit-user-drag:none;pointer-events:none}
.roi{position:absolute;border:2px solid var(--roi);pointer-events:none}
.roi.draft{border-style:dashed}.roi .h{position:absolute;width:14px;height:14px;background:var(--roi);border:2px solid #000;border-radius:2px}
.h.lt{left:-8px;top:-8px}.h.rt{right:-8px;top:-8px}.h.lb{left:-8px;bottom:-8px}.h.rb{right:-8px;bottom:-8px}
.layer{position:absolute;line-height:1.35;text-shadow:0 1px 2px #000,0 0 6px #000;pointer-events:none}
.layer>*{pointer-events:auto}
.top{left:0;right:0;top:0;padding:.6em .8em;display:flex;gap:1.2em;align-items:flex-start}
.state{white-space:nowrap}.state b{font-size:1.5em}.on{color:var(--ok)}.off,.bad{color:var(--alarm)}.good{color:var(--ok)}.unknown{color:var(--warn)}.none{color:var(--muted)}
.muted{color:var(--muted)}.state .muted{color:#dfe4e8}.spacer{flex:1}
.controls{display:flex;gap:.4em}
button,select{font:inherit;font-weight:700;white-space:nowrap;padding:.45em .9em;border-radius:.5em;border:0;color:#fff;cursor:pointer;text-shadow:none}
.stop{background:#c4271d}.go{background:#1f8a4c}.plain{background:rgba(255,255,255,.18)}select{background:#222;font-weight:400}
.bottom{left:0;right:0;bottom:0;padding:.6em .8em}
.log{max-height:var(--log-height,30vh);overflow-y:auto;overscroll-behavior:contain;scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.35) transparent;-webkit-mask-image:linear-gradient(to bottom,transparent 0,#000 1.2em);mask-image:linear-gradient(to bottom,transparent 0,#000 1.2em);padding-top:1.2em}
table{border-collapse:collapse}td{padding:0 .9em 0 0;white-space:nowrap;vertical-align:top}
td.reason{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:45vw}td.num{font-variant-numeric:tabular-nums}
.edit{left:0;right:0;top:3.2em;padding:0 .8em;display:flex;gap:.4em;align-items:center;flex-wrap:wrap}
.msg{right:.8em;bottom:.6em;color:var(--muted);max-width:40%;text-align:right}
.stage.editing{cursor:crosshair}
</style></head><body>
<div class="stage" id="stage"><img id="frame" alt="監視の画像" draggable="false">
<div class="roi" id="roi" hidden></div><div class="roi draft" id="draft" hidden><i class="h lt"></i><i class="h rt"></i><i class="h lb"></i><i class="h rb"></i></div>
<div class="layer top">
<div class="state"><span class="muted">ベルト</span> <b id="belt">-</b></div>
<div class="state"><span class="muted">警報</span> <b id="incident">-</b></div>
<div class="state" id="waitBox" hidden><b class="unknown">起動待ち</b> <span id="deps"></span></div>
<span class="spacer"></span>
<div class="controls"><button class="stop" id="stop">停止</button><button class="go" id="resume">再開</button><button class="plain" id="roiButton">ROI</button></div>
</div>
<div class="layer edit" id="edit" hidden><span>枠の外をドラッグで描く・中で動かす・四隅で大きさ</span>
<select id="rot"><option value="0">向き 0度</option><option value="180">向き 180度</option></select>
<button class="plain" id="full">全景</button><button class="go" id="apply">適用</button><button class="plain" id="cancel">やめる</button></div>
<div class="layer bottom"><div class="log" id="log"><table><tbody id="results"></tbody></table></div><div id="waitReasons"></div></div>
<div class="layer msg" id="msg"></div>
</div>
<script>
const $=id=>document.getElementById(id);let settings=null,editing=false,mode=null,start=null,orig=null,roi=null,loading=false,opened=false;  // URLの#roiで、最初からROIの調整を開く
const EVENT={jam:'詰まりで停止',manual:'画面から停止',monitoring_error:'監視の異常で停止'};
// 判定は4種類。VLMの答えが、監視にとって何を意味するかで分ける
const VERDICT={duck_blocked:['詰まり','bad'],thumbs_up:['good','good'],clear:['none','none'],none:['none','none'],uncertain:['unknown','unknown']};
const esc=t=>String(t==null?'':t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function box(el,r){if(!r){el.hidden=true;return}el.hidden=false;Object.assign(el.style,{left:r[0]*100+'%',top:r[1]*100+'%',width:(r[2]-r[0])*100+'%',height:(r[3]-r[1])*100+'%'})}
// 縦横の比を保ってウィンドウいっぱいに広げる。重ねた文字とROIの枠を、画像とずらさないため
function fit(){const img=$('frame');if(!img.naturalWidth)return;const a=img.naturalWidth/img.naturalHeight,w=Math.min(innerWidth,innerHeight*a);img.style.width=w+'px';img.style.height=w/a+'px';$('log').style.setProperty('--log-height',Math.round(w/a/3)+'px')}
addEventListener('resize',fit);
function refresh(){if(loading)return;loading=true;const img=new Image();img.onload=()=>{$('frame').src=img.src;$('frame').onload=fit;loading=false};img.onerror=()=>{loading=false};img.src='/frame.jpg?'+Date.now()}
async function tick(){try{const s=await (await fetch('/status.json')).json(),st=s.status;settings=s.settings;
$('belt').textContent={on:'ON',off:'OFF'}[st.belt]||st.belt||'-';$('belt').className=st.belt==='on'?'on':st.belt==='off'?'off':'unknown';
// 起動待ち: カメラ・VLM・conveyor-mcpのどれを待っているかと、その理由を出す
const NAMES={camera:'カメラ',vlm:'VLM',mcp:'コンベアMCP'},deps=st.dependencies||{},waiting=st.phase==='waiting';
$('waitBox').hidden=!waiting;
$('deps').innerHTML=Object.entries(deps).map(([k,d])=>'<b class="'+(d.ok?'good':'bad')+'">'+(d.ok?'✓':'✗')+' '+esc(NAMES[k]||k)+'</b>').join(' ');
$('waitReasons').innerHTML=waiting?Object.entries(deps).filter(([,d])=>!d.ok).map(([k,d])=>'<div><b class="bad">'+esc(NAMES[k]||k)+'</b> '+esc(d.error||'応答なし')+'</div>').join(''):'';
const inc=st.incident;$('incident').textContent=inc?(EVENT[inc.result.event]||inc.result.event)+'（👍か再開）':'なし';$('incident').className=inc?'bad':'';
// 新しい順。古い行を読んでいる間に新しい行が上に足されても、読んでいる位置をずらさない
const log=$('log'),before=log.scrollHeight,top=log.scrollTop;
$('results').innerHTML=(s.results||[]).map(r=>{const[label,cls]=VERDICT[r.result]||[r.result,'unknown'];
return '<tr><td class="num">'+esc(new Date(r.captured_at).toLocaleTimeString())+'</td><td class="'+cls+'"><b>'+esc(label)+'</b></td><td class="num">'+(r.seconds!=null?r.seconds.toFixed(1)+'s':'')+'</td><td class="reason'+(r.reason?'':' none')+'">'+esc(r.reason||(r.kind==='gesture'?'👍の観察':''))+'</td></tr>'}).join('');if(top>0)log.scrollTop=top+log.scrollHeight-before;
if(s.last_command&&Date.now()/1000-s.last_command.finished_at<5)$('msg').textContent=(s.last_command.ok?'':'失敗: ')+s.last_command.message;else $('msg').textContent='';
box($('roi'),settings.detail_roi);if(!editing){roi=settings.detail_roi;$('rot').value=String(settings.rotation)}
if(location.hash==='#roi'&&!opened){opened=true;setEditing(true)}
}catch(e){$('msg').textContent='監視に届きません'}refresh()}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)$('msg').textContent='エラー: '+(await r.json()).error}
function setEditing(on){editing=on;$('edit').hidden=!on;$('stage').classList.toggle('editing',on);$('log').parentElement.hidden=on;roi=settings&&settings.detail_roi;box($('draft'),on?roi:null)}
// ROIの調整: 枠の外は新しく描く、枠の中は動かす、四隅は大きさを変える
const HANDLE=0.035,MIN=0.02,clamp=v=>Math.min(1,Math.max(0,v));
function hit(p,r){if(!r)return null;const[l,t,rr,b]=r,near=(a,c)=>Math.abs(a-c)<HANDLE;
for(const[c,x,y]of[['lt',l,t],['rt',rr,t],['lb',l,b],['rb',rr,b]])if(near(p[0],x)&&near(p[1],y))return c;
return p[0]>l&&p[0]<rr&&p[1]>t&&p[1]<b?'move':null}
function edited(p){if(mode==='draw')return[Math.min(start[0],p[0]),Math.min(start[1],p[1]),Math.max(start[0],p[0]),Math.max(start[1],p[1])];
if(mode==='move'){const w=orig[2]-orig[0],h=orig[3]-orig[1],l=Math.min(clamp(orig[0]+p[0]-start[0]),1-w),t=Math.min(clamp(orig[1]+p[1]-start[1]),1-h);return[l,t,l+w,t+h]}
const r=[...orig];r[mode[0]==='l'?0:2]=p[0];r[mode[1]==='t'?1:3]=p[1];return[Math.min(r[0],r[2]),Math.min(r[1],r[3]),Math.max(r[0],r[2]),Math.max(r[1],r[3])]}
function pos(e){const r=$('frame').getBoundingClientRect();return[clamp((e.clientX-r.left)/r.width),clamp((e.clientY-r.top)/r.height)]}
const stage=$('stage');
stage.addEventListener('pointerdown',e=>{if(!editing||e.target.closest('button,select'))return;e.preventDefault();const p=pos(e);mode=hit(p,roi)||'draw';start=p;orig=roi;stage.setPointerCapture(e.pointerId)});
stage.addEventListener('pointermove',e=>{if(!editing)return;const p=pos(e);if(!mode){const h=hit(p,roi);stage.style.cursor=h==='move'?'move':h?(h==='lt'||h==='rb'?'nwse-resize':'nesw-resize'):'crosshair';return}
const r=edited(p);if(r[2]-r[0]<MIN||r[3]-r[1]<MIN)return;roi=r;box($('draft'),r)});
['pointerup','pointercancel'].forEach(t=>stage.addEventListener(t,()=>{mode=null}));
$('roiButton').onclick=()=>setEditing(!editing);$('cancel').onclick=()=>setEditing(false);
$('full').onclick=()=>{roi=null;box($('draft'),null)};
$('apply').onclick=async()=>{await post('/settings',{detail_roi:roi,rotation:Number($('rot').value)});setEditing(false)};
$('stop').onclick=()=>post('/command',{action:'stop'});$('resume').onclick=()=>post('/command',{action:'resume'});
tick();setInterval(tick,1000);
</script></body></html>
'''
