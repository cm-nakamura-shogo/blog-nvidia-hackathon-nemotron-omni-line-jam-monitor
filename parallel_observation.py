"""👍の観察と、最新画像の障害物の観察。VLMへの同時の要求は2本まで。"""
from concurrent.futures import ThreadPoolExecutor, wait
import time
from observation_pipeline import observe, validate_observation
from detail_observation import inspect_blockage, make_detail_crop


class ParallelObserver:
    """VLMへの要求を、最大2本のスレッドで送る。古い画像を推論のキューに積まない。"""

    def __init__(self, url, model, roi, reason_lang='en'):
        """roiは障害物を見る範囲（Noneなら全景）。監視の画面から変えるので、呼ぶ側が書き換えてよい。"""
        self.url, self.model, self.roi, self.reason_lang = url, model, roi, reason_lang
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='vlm')
        self.pending = []

    def close(self):
        """送っている推論の終わりを待ってから、スレッドを止める。"""
        self.pool.shutdown(wait=True, cancel_futures=True)

    def run_independent(self, frame, latest_frame, on_blockage, on_tick=None, gesture=True, blockage=True):
        """frameの👍の観察と、最新画像の障害物の観察を行い、(観察, メタ情報, 切り出し画像)を返す。

        制御スレッドで呼ぶ前提。on_blockageとon_tickもこのスレッドで動くので、MCPの指令や警報の更新と競合しない。
        on_tickは約50msごとに呼ぶ（画面から積まれた操作を、VLMの応答を待つ間も実行するため）。
        gesture / blockageで観察を選ぶ。監視は、警報がない間は障害物だけ、警報中は👍だけを頼む。
        AGX OrinのVLMは2本を同時に受けると1本ずつより遅いため。gestureなしなら、障害物の答えが1回返った時点で戻る。
        """
        from concurrent.futures import FIRST_COMPLETED
        if not hasattr(self, 'block_future'):
            self.block_future = None
            self.last_block_time = None
            self.block_cache = {}
        wait(self.pending)
        started = time.monotonic()
        def timed(fn, *args):
            """fnを呼び、(答え, この呼び出しの始まりと終わりの時刻)を返す。"""
            begin = time.monotonic()
            value = fn(*args)
            return value, {'started_seconds': begin-started,
                           'finished_seconds': time.monotonic()-started}
        # 👍も障害物と同じROIで見る。全景だと手が小さく写り、見落とすため
        futures = {self.pool.submit(timed, observe, self.url, self.model, make_detail_crop(frame['jpeg'], self.roi)): 'full'} if gesture else {}
        self.pending = list(futures)
        results, timings = {}, {}
        answered = False
        # gestureなしなら、障害物の答えが1回返るまで（見る画像がなければそこまで）回す
        while futures or (not gesture and blockage and not answered):
            if on_tick:
                on_tick()
            if self.block_future is None and blockage and not (not gesture and answered):
                candidate = latest_frame()
                if candidate and candidate['time'] != self.last_block_time:
                    self.last_block_time = candidate['time']
                    self.block_frame = candidate
                    crop = make_detail_crop(candidate['jpeg'], self.roi)
                    self.block_started = time.monotonic()
                    self.block_future = self.pool.submit(inspect_blockage, self.url, self.model, crop, self.roi is None, self.reason_lang)
            active = list(futures)
            if self.block_future is not None:
                active.append(self.block_future)  # 頼んだ後に警報になっても、送った分の答えは受け取る
            elif not futures:
                break  # 新しい画像がない。次の撮影まで待たずに監視の本体へ戻す
            done, _ = wait(active, timeout=.05, return_when=FIRST_COMPLETED)
            if self.block_future in done:
                block_future = self.block_future
                self.block_future = None
                answer, raw, usage = block_future.result()
                elapsed = time.monotonic()-self.block_started
                self.block_cache[self.block_frame['time']] = (answer, raw, usage, elapsed)
                while len(self.block_cache)>16:
                    del self.block_cache[next(iter(self.block_cache))]
                on_blockage(answer, self.block_frame, raw, usage, elapsed)
                answered = True
            for future in done:
                if future in futures:
                    name = futures.pop(future)
                    results[name], timings[name] = future.result()
        if gesture:
            raw, usage = results['full']
            obs = validate_observation(raw)
        else:
            raw, usage = None, None
            obs = {'operator_gesture': 'skipped'}  # thumbs_upではないので、見ていない間に警報が解けることはない
        crop = make_detail_crop(frame['jpeg'], self.roi)
        meta = dict(raw_response=raw, usage=usage, inference_timings=timings,
                    inference_wall_seconds=time.monotonic()-started)
        # 別の画像の障害物の答えを、この画像の答えとして付けない（撮った時刻が違うと状況も違うため）
        same_frame = self.block_cache.get(frame['time'])
        if same_frame:
            obs['detail_blockage'], meta['blockage_raw_response'], meta['blockage_usage'], meta['blockage_seconds'] = same_frame
        elif not blockage:
            obs['detail_blockage'] = {'blockage':'uncertain','reason':'警報中は障害物を観察しない（👍だけを見る）'}
        else:
            obs['detail_blockage'] = {'blockage':'uncertain','reason':'このフレームの障害物観察は未完了。独立監視のログを参照'}
        return obs, meta, crop
