import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from PIL import Image
import parallel_observation as module

OBS = dict(operator_gesture='none')

def jpeg():
    buf=io.BytesIO();Image.new('RGB',(640,400)).save(buf,format='JPEG');return buf.getvalue()


def test_blockage_lane_processes_new_frames_while_full_and_positions_are_still_running(monkeypatch):
    release=threading.Event()
    def full(*args):
        assert release.wait(3), 'independent blockage lane stalled behind full frame'
        return json.dumps(OBS),None
    monkeypatch.setattr(module,'observe',full)
    monkeypatch.setattr(module,'inspect_blockage',lambda *a:({'blockage':'clear','reason':'none'},'{}',None))
    frame=jpeg();counter=[0];seen=[]
    def latest():
        counter[0]+=1
        return {'time':counter[0], 'file':str(counter[0]), 'jpeg':frame}
    def callback(block,source,*args):
        seen.append(source['time'])
        if len(seen)>=4:release.set()
    observer=module.ParallelObserver('unused','model',(.2,.3,.5,.9))
    try:
        obs,_,_=observer.run_independent({'time':0,'jpeg':frame},latest,callback)
        assert seen[:4]==[1,2,3,4]
        assert obs['detail_blockage']['blockage']=='uncertain'  # no cross-frame merge
    finally:release.set();observer.close()


def test_exclusive_lanes_send_one_kind_of_request(monkeypatch):
    calls=[]
    monkeypatch.setattr(module,'observe',lambda *a:(calls.append('gesture'),(json.dumps(OBS),None))[1])
    monkeypatch.setattr(module,'inspect_blockage',lambda *a:(calls.append('blockage'),({'blockage':'duck_blocked','reason':'hand'},'{}',None))[1])
    frame={'time':1,'file':'1','jpeg':jpeg()};seen=[]
    observer=module.ParallelObserver('unused','model',(.2,.3,.5,.9))
    try:
        obs,_,_=observer.run_independent(frame,lambda:frame,lambda b,*a:seen.append(b),gesture=False,blockage=True)
        assert calls==['blockage'] and obs['operator_gesture']=='skipped'
        assert obs['detail_blockage']['blockage']=='duck_blocked', 'the answer for this frame is attached'
        calls.clear()
        obs,_,_=observer.run_independent({**frame,'time':2},lambda:{**frame,'time':2},lambda *a:pytest.fail('no blockage while held'),gesture=True,blockage=False)
        assert calls==['gesture'] and obs['operator_gesture']=='none'
        assert obs['detail_blockage']['blockage']=='uncertain'
    finally:observer.close()
