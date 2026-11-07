"""VLMの全景の要求をわざと止めている間も、MCPの停止が終わること。

ベルトは工場側が回している状態から始める。監視は起動時にも終了時にもコンベアに触らないこと。"""
import io,json,subprocess,sys,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from PIL import Image
import pytest


@pytest.mark.parametrize('roi', [[], ['--detail-roi','.2','.3','.5','.9']])
def test_stop_does_not_wait_for_slow_full_frame_request(tmp_path, roi):
    buf=io.BytesIO();Image.new('RGB',(80,60),'green').save(buf,format='JPEG')
    stopped=threading.Event();running=threading.Event();running.set()
    slow_entered=threading.Event();commands=[];counts={'full':0,'narrative':0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,value):
            self.send_response(200);self.end_headers();self.wfile.write(json.dumps(value).encode())
        def do_GET(self):
            self.send_response(200);self.send_header('X-Captured-At',str(time.time()));self.end_headers();self.wfile.write(buf.getvalue())
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path=='/mcp':
                command=payload['params']['name']
                if command!='get_conveyor_status':commands.append(command)
                if command=='start_conveyor':running.set()
                if command=='stop_conveyor' and running.is_set():running.clear();stopped.set()
                self.send({'result':{'content':[{'type':'text','text':json.dumps(dict(simulation=False,closed=False,fault=None,last_acknowledged_running=running.is_set()))}]}})
                return
            properties=payload['response_format'].get('json_schema',{}).get('schema',{}).get('properties',{})
            if 'operator_gesture' in properties:
                counts['full']+=1;n=counts['full']
                if n==3:
                    slow_entered.set()
                    assert stopped.wait(6), 'MCP OFF blocked behind full-frame inference'
                output=dict(operator_gesture='none')
            elif 'blockage' in properties:
                output={'blockage':'duck_blocked','reason':'barrier'}
            else:
                counts['narrative']+=1
                output=dict(status='normal',event='none',location='unknown',reason='normal',evidence='test')
            self.send({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}]})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}'
    token=tmp_path/'token';token.write_text('test')
    process=subprocess.Popen([sys.executable,str(Path(__file__).with_name('monitor.py')),
        '--control','--mcp-url',url+'/mcp','--token-file',str(token),
        '--snapshot-url',url+'/camera','--vlm-url',url+'/v1/chat/completions',
        '--interval','.5','--jam-seconds','1','--out',str(tmp_path/'runs')]+roi,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert slow_entered.wait(8)
        assert stopped.wait(3), 'should stop before slow response can finish'
        time.sleep(.1)
        process.terminate();out,err=process.communicate(timeout=10)
        assert process.returncode==0,out+err
        assert commands==['stop_conveyor'], 'only the jam stop; no startup OFF/ON and no OFF at exit'
        assert counts['narrative']==0
        rows=[json.loads(s) for s in next((tmp_path/'runs').glob('*/judgments.jsonl')).read_text().splitlines()]
        assert any(row.get('phase')=='early_blockage' and row['stop_acknowledged'] for row in rows)
    finally:
        stopped.set()
        if process.poll() is None:process.kill();process.wait()
        server.shutdown();server.server_close()


def test_belt_stopped_by_factory_is_not_a_jam(tmp_path):
    """工場側が止めたベルトで、障害物の前にアヒルがいても、詰まりにせず、MCPに何も送らないこと。"""
    buf=io.BytesIO();Image.new('RGB',(80,60),'green').save(buf,format='JPEG')
    commands=[];blockages=threading.Event();counts={'blockage':0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,value):
            self.send_response(200);self.end_headers();self.wfile.write(json.dumps(value).encode())
        def do_GET(self):
            self.send_response(200);self.send_header('X-Captured-At',str(time.time()));self.end_headers();self.wfile.write(buf.getvalue())
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path=='/mcp':
                command=payload['params']['name']
                if command!='get_conveyor_status':commands.append(command)
                self.send({'result':{'content':[{'type':'text','text':json.dumps(dict(simulation=False,closed=False,fault=None,last_acknowledged_running=False))}]}})
                return
            properties=payload['response_format'].get('json_schema',{}).get('schema',{}).get('properties',{})
            if 'blockage' in properties:
                counts['blockage']+=1
                if counts['blockage']>=6:blockages.set()
                output={'blockage':'duck_blocked','reason':'barrier'}
            else:
                output=dict(operator_gesture='none')
            self.send({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}]})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}'
    token=tmp_path/'token';token.write_text('test')
    process=subprocess.Popen([sys.executable,str(Path(__file__).with_name('monitor.py')),
        '--control','--mcp-url',url+'/mcp','--token-file',str(token),
        '--snapshot-url',url+'/camera','--vlm-url',url+'/v1/chat/completions',
        '--interval','.5','--jam-seconds','1','--out',str(tmp_path/'runs')],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert blockages.wait(10), 'blockage observations should keep running'
        process.terminate();out,err=process.communicate(timeout=10)
        assert process.returncode==0,out+err
        assert commands==[], 'the monitor must not command a belt the factory stopped'
        assert not (tmp_path/'runs'/'alert_state.json').exists() or '"incident": null' in (tmp_path/'runs'/'alert_state.json').read_text()
    finally:
        if process.poll() is None:process.kill();process.wait()
        server.shutdown();server.server_close()


def test_restart_by_another_path_clears_alarm_and_jam_stops_again(tmp_path):
    """監視が止めた後に工場側がベルトを回すと、警報が解け、次の詰まりでまた止まること。"""
    buf=io.BytesIO();Image.new('RGB',(80,60),'green').save(buf,format='JPEG')
    commands=[];running=threading.Event();running.set();second_stop=threading.Event();times={}
    def restart():
        times['restarted']=time.monotonic();running.set()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,value):
            self.send_response(200);self.end_headers();self.wfile.write(json.dumps(value).encode())
        def do_GET(self):
            self.send_response(200);self.send_header('X-Captured-At',str(time.time()));self.end_headers();self.wfile.write(buf.getvalue())
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path=='/mcp':
                command=payload['params']['name']
                if command!='get_conveyor_status':commands.append(command)
                if command=='stop_conveyor':
                    running.clear()
                    if commands.count('stop_conveyor')==1:
                        threading.Timer(1.5,restart).start()  # 監視以外がベルトを回す
                    else:times['second_stop']=time.monotonic();second_stop.set()
                self.send({'result':{'content':[{'type':'text','text':json.dumps(dict(simulation=False,closed=False,fault=None,last_acknowledged_running=running.is_set()))}]}})
                return
            properties=payload['response_format'].get('json_schema',{}).get('schema',{}).get('properties',{})
            output={'blockage':'duck_blocked','reason':'barrier'} if 'blockage' in properties else dict(operator_gesture='none')
            self.send({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}]})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}'
    token=tmp_path/'token';token.write_text('test')
    process=subprocess.Popen([sys.executable,str(Path(__file__).with_name('monitor.py')),
        '--control','--mcp-url',url+'/mcp','--token-file',str(token),
        '--snapshot-url',url+'/camera','--vlm-url',url+'/v1/chat/completions',
        '--interval','.5','--jam-seconds','1','--out',str(tmp_path/'runs')],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert second_stop.wait(20), 'the restarted belt must be protected again'
        assert times['second_stop']-times['restarted']>=5, '再開の直後（--resume-grace の既定5秒）は詰まりで止めない'
        process.terminate();out,err=process.communicate(timeout=10)
        assert process.returncode==0,out+err
        assert commands==['stop_conveyor','stop_conveyor'], 'the monitor never starts a belt itself here'
        rows=[json.loads(s) for s in next((tmp_path/'runs').glob('*/judgments.jsonl')).read_text().splitlines()]
        assert sum(row.get('transition')=='external_restart' for row in rows)==1
    finally:
        if process.poll() is None:process.kill();process.wait()
        server.shutdown();server.server_close()


def test_screen_stop_resume_and_settings_run_in_the_control_thread(tmp_path):
    """VLMの応答が遅くても、画面からの停止・再開・ROIと向きの変更が制御スレッドで実行されること。

    ベルトは工場側が回している状態から始める。起動時にも終了時にもコンベアに触らないこと。"""
    import socket
    import urllib.request
    buf=io.BytesIO();Image.new('RGB',(80,60),'green').save(buf,format='JPEG')
    commands=[];changed=threading.Event();running=threading.Event();running.set()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,value):
            self.send_response(200);self.end_headers();self.wfile.write(json.dumps(value).encode())
        def do_GET(self):
            self.send_response(200);self.send_header('X-Captured-At',str(time.time()));self.end_headers();self.wfile.write(buf.getvalue())
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path=='/mcp':
                command=payload['params']['name']
                if command=='start_conveyor':running.set()
                if command=='stop_conveyor':running.clear()
                if command!='get_conveyor_status':commands.append(command);changed.set()
                self.send({'result':{'content':[{'type':'text','text':json.dumps(dict(simulation=False,closed=False,fault=None,last_acknowledged_running=running.is_set()))}]}})
                return
            time.sleep(1)  # 遅いVLM。画面の操作はこれを待たないこと
            properties=payload['response_format'].get('json_schema',{}).get('schema',{}).get('properties',{})
            output={'blockage':'clear','reason':'no duck'} if 'blockage' in properties else dict(operator_gesture='none')
            self.send({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}]})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}'
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));ui_port=s.getsockname()[1]
    ui=f'http://127.0.0.1:{ui_port}'
    token=tmp_path/'token';token.write_text('test')
    process=subprocess.Popen([sys.executable,str(Path(__file__).with_name('monitor.py')),
        '--control','--mcp-url',url+'/mcp','--token-file',str(token),
        '--snapshot-url',url+'/camera','--vlm-url',url+'/v1/chat/completions',
        '--interval','.5','--jam-seconds','1','--out',str(tmp_path/'runs'),'--ui-host','127.0.0.1','--ui-port',str(ui_port)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    def post(path,body):
        req=urllib.request.Request(ui+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=5) as r:return r.status
    def incident(limit=3):
        # 警報はMCPの応答の後に保存するので、MCPが要求を受けた時点より少し遅れる
        path=tmp_path/'runs'/'alert_state.json';end=time.time()+limit
        while time.time()<end:
            if path.exists():
                value=json.loads(path.read_text())['incident']
                if value is not None:return value
            time.sleep(.05)
        return None
    def cleared(limit=3):
        path=tmp_path/'runs'/'alert_state.json';end=time.time()+limit
        while time.time()<end:
            if json.loads(path.read_text())['incident'] is None:return True
            time.sleep(.05)
        return False
    def belt(expected,limit=3):
        end=time.time()+limit
        while time.time()<end:
            try:
                with urllib.request.urlopen(ui+'/status.json',timeout=5) as r:
                    if json.load(r)['status'].get('belt')==expected:return True
            except OSError:
                pass  # 画面はプロセスの起動から少し遅れて立ち上がる
            time.sleep(.1)
        return False
    def wait_for(expected,limit):
        end=time.time()+limit
        while time.time()<end:
            if commands==expected:return True
            changed.wait(.1);changed.clear()
        return False
    try:
        assert belt('on',15), 'the screen should show the belt state read from conveyor-mcp'
        assert commands==[], 'startup must not touch the conveyor'
        sent=time.time();assert post('/command',{'action':'stop'})==202
        assert wait_for(['stop_conveyor'],3), commands
        assert time.time()-sent<1, 'stop must not wait for the slow VLM response'
        assert (incident() or {}).get('result',{}).get('event')=='manual'
        def kinds():
            try:
                with urllib.request.urlopen(ui+'/status.json',timeout=5) as r:return [x['kind'] for x in json.load(r)['results']]
            except OSError:return []
        end=time.time()+10  # 👍は警報中だけ観察する
        while time.time()<end and 'gesture' not in kinds():time.sleep(.2)
        assert kinds()[0]=='gesture' and 'gesture' not in kinds()[1:], 'no thumbs-up was asked before the alarm'
        assert post('/command',{'action':'resume'})==202
        assert wait_for(['stop_conveyor','start_conveyor'],3), commands
        assert cleared()
        assert post('/settings',{'detail_roi':[0.5,0.1,0.9,0.6],'rotation':0})==202
        end=time.time()+3
        while time.time()<end and not (tmp_path/'runs'/'monitor_settings.json').exists():time.sleep(.05)
        assert json.loads((tmp_path/'runs'/'monitor_settings.json').read_text())=={'detail_roi':[0.5,0.1,0.9,0.6],'rotation':0}
        with urllib.request.urlopen(ui+'/status.json',timeout=5) as r:status=json.load(r)
        assert status['settings']['rotation']==0 and belt('on')
        end=time.time()+10  # 模擬のVLMは1回に1秒かかり、再開より前の結果は捨てる
        while time.time()<end and {r['kind'] for r in status['results']}!={'blockage','gesture'}:
            time.sleep(.2)
            with urllib.request.urlopen(ui+'/status.json',timeout=5) as r:status=json.load(r)
        assert {r['kind'] for r in status['results']}=={'blockage','gesture'}, 'the screen should list both inference results'
        assert all(r['result'] in ('clear','none') for r in status['results'])
        process.terminate();out,err=process.communicate(timeout=10)
        assert process.returncode==0,out+err
        assert commands==['stop_conveyor','start_conveyor'], 'exit must not touch the conveyor'

        rows=[json.loads(s) for s in next((tmp_path/'runs').glob('*/judgments.jsonl')).read_text().splitlines()]
        assert [r['transition'] for r in rows if str(r.get('transition','')).startswith('screen_')]==['screen_stop','screen_resume','screen_settings']
    finally:
        if process.poll() is None:process.kill();process.wait()
        server.shutdown();server.server_close()


def test_waits_for_the_vlm_without_an_alarm_and_then_starts(tmp_path):
    """VLMがまだ立ち上がっていない間は推論せず、警報にもせず、コンベアにも触らない。応答したら監視を始めること。"""
    import socket
    import urllib.request
    buf=io.BytesIO();Image.new('RGB',(80,60),'green').save(buf,format='JPEG')
    commands=[];vlm_up=threading.Event();chats=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,value):
            self.send_response(200);self.end_headers();self.wfile.write(json.dumps(value).encode())
        def do_GET(self):
            if self.path.endswith('/models'):
                if not vlm_up.is_set():self.send_error(503,'Loading model');return
                self.send({'data':[{'id':'line-base'}]});return
            self.send_response(200);self.send_header('X-Captured-At',str(time.time()));self.end_headers();self.wfile.write(buf.getvalue())
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path=='/mcp':
                command=payload['params']['name']
                if command!='get_conveyor_status':commands.append(command)
                self.send({'result':{'content':[{'type':'text','text':json.dumps(dict(simulation=False,closed=False,fault=None,last_acknowledged_running=True))}]}})
                return
            chats.append(1)
            self.send({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'blockage':'clear','reason':'アヒルはいない'})}}]})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}'
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));ui_port=s.getsockname()[1]
    token=tmp_path/'token';token.write_text('test')
    process=subprocess.Popen([sys.executable,str(Path(__file__).with_name('monitor.py')),
        '--control','--mcp-url',url+'/mcp','--token-file',str(token),
        '--snapshot-url',url+'/camera','--vlm-url',url+'/v1/chat/completions',
        '--interval','.5','--out',str(tmp_path/'runs'),'--ui-host','127.0.0.1','--ui-port',str(ui_port),'--ready-interval','.3'],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    def status():
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{ui_port}/status.json',timeout=5) as r:return json.load(r)['status']
        except OSError:return {}
    def until(pred,limit=10):
        end=time.time()+limit
        while time.time()<end:
            st=status()
            if pred(st):return st
            time.sleep(.2)
        return status()
    try:
        st=until(lambda st:st.get('phase')=='waiting' and st.get('dependencies',{}).get('camera',{}).get('ok'))
        assert st.get('phase')=='waiting', st
        assert st['dependencies']['vlm']['ok'] is False and st['dependencies']['mcp']['ok'] is True
        time.sleep(1.5)
        assert chats==[] and commands==[], 'no inference and no conveyor command while waiting'
        assert not list((tmp_path/'runs').glob('*/frame_*.jpg')), 'frames are not saved while waiting'
        vlm_up.set()
        st=until(lambda st:st.get('phase')=='monitoring' and st.get('blockage'))
        assert st.get('phase')=='monitoring' and st['blockage']['blockage']=='clear', st
        process.terminate();out,err=process.communicate(timeout=10)
        assert process.returncode==0,out+err
        assert commands==[] and '[起動待ち] 応答を待っています: vlm' in out
        assert not (tmp_path/'runs'/'alert_state.json').exists() or '"incident": null' in (tmp_path/'runs'/'alert_state.json').read_text()
    finally:
        if process.poll() is None:process.kill();process.wait()
        server.shutdown();server.server_close()
