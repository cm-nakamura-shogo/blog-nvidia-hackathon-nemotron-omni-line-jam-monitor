"""詰まり監視の警報の保持と解除。モデルの答えではなく、決まった規則で判断する。警報の解除だけでは設備を動かさない。"""
import json
import os
from pathlib import Path


class AlertState:
    """警報を1件だけ保持し、ファイルに保存する。👍で解除するときの回数の数え方も持つ。

    警報は監視を起動し直しても残す（解決していない詰まりを、再起動で無かったことにしないため）。
    """

    def __init__(self, path=None, max_gap=6, on_latch=None, thumbs_required=2):
        """pathに保存した警報があれば読む。中身が不正ならValueErrorを出す。max_gapより間が空いた👍は続けて数えない。"""
        # 警報の後、先に'none'を1回見てから数えるthumbs_upの回数。monitorは--thumbs-countを渡す
        self.thumbs_required = thumbs_required
        self.path = Path(path) if path else None
        # 新しく警報が立ったときに呼ぶ（MQTTへの知らせ）
        self.on_latch = on_latch
        self.max_gap = max_gap
        self.incident = None
        self.armed = False
        self.count = 0
        self.last_time = None
        if self.path and self.path.exists():
            saved = json.loads(self.path.read_text())
            if set(saved) != {'incident'} or (saved['incident'] is not None and not isinstance(saved['incident'], dict)):
                raise ValueError('警報状態ファイルが不正です')
            self.incident = saved['incident']

    def save(self):
        """警報をファイルに書く。書きかけのファイルが残らないよう、一時ファイルに書いてから置き換える。"""
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'incident': self.incident}, ensure_ascii=False))
            os.replace(temporary, self.path)

    def retire_removed_events(self):
        """削除した検出（持ち去り、個数減少、転落）の古い警報を解除する。今の監視では解除できないまま残るため。"""
        if self.incident and self.incident['result']['event'] in {'human_removal', 'count_loss', 'fall'}:
            self.acknowledge()

    @property
    def waiting_start(self):
        """以前の版の「起動時の👍待ち」の警報か。今は起動時に解除して移行する。"""
        return self.incident is not None and self.incident['result']['event'] == 'awaiting_start'

    def wait_for_start(self):
        """以前の版の「起動時の👍待ち」を始める。今の監視は呼ばない（古い保存ファイルとの互換のため残す）。"""
        if self.incident is None or self.waiting_start:
            self.incident = {'detected_at': None, 'result': {
                'status': 'waiting_start', 'event': 'awaiting_start', 'reason': '👍で開始待ち'}}
            self.invalidate()
            self.armed = True
            self.save()

    def latch(self, result, timestamp):
        """resultが異常（status abnormal）で、警報がまだなければ警報にして保存し、on_latchを呼ぶ。

        警報中の次の異常では何もしない（同じ停止を何度も知らせないため）。
        """
        if result['status'] == 'abnormal' and (self.incident is None or self.waiting_start):
            self.incident = {'detected_at': timestamp, 'result': result}
            self.invalidate()
            self.save()
            if self.on_latch:
                self.on_latch(self.incident)

    def invalidate(self):
        """👍の数えかけを捨てる。次は👍なしを見てから数え直す。"""
        self.armed = False
        self.count = 0
        self.last_time = None

    def observe_gesture(self, observation, timestamp):
        """警報中の👍の観察を1件数え、解除してよければTrueを返す。

        警報の後に一度👍なし（none）を見てから、thumbs_upがthumbs_required回続いたら解除する。
        Trueを返すだけで、解除（acknowledge）と再開は呼ぶ側が行う。
        """
        if self.incident is None:
            return False
        if self.last_time is not None:
            if timestamp <= self.last_time:
                return False  # 同じ画像や古い画像を、もう1回の👍として数えない
            if timestamp - self.last_time > self.max_gap:
                self.invalidate()
        self.last_time = timestamp
        gesture = observation['operator_gesture']
        if gesture == 'none':
            self.armed = True  # 警報の前から出ていた👍で解除しないよう、一度👍なしを見てから数える
            self.count = 0
            return False
        if gesture != 'thumbs_up':
            self.count = 0  # uncertainなどは👍ではないので、続けて数えるのをやめる
            return False
        if not self.armed:
            return False
        self.count += 1
        if self.count < self.thumbs_required:
            return False
        return True  # 呼ぶ側は、再開（MCPのON応答）を確かめてからacknowledgeする

    def acknowledge(self):
        """警報を解除して保存する。設備は動かさない。"""
        self.incident = None
        self.invalidate()
        self.save()
