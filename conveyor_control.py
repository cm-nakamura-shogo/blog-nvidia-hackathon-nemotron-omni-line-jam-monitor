"""監視からconveyor-mcpを呼ぶ。呼べるツールはコードで決めた3つだけで、モデルの答えでツールや引数を選ばない。"""
import json
import urllib.request


class ConveyorControl:
    """conveyor-mcpの開始・停止・状態の読み取り。応答が実機の確かな受付でなければ例外にする。"""

    def __init__(self, url, token):
        if not token.strip():
            raise ValueError('MCPトークンが空です')
        self.url, self.token = url, token.strip()

    def call(self, tool, timeout=5):
        """toolを1回呼び、状態を返す。実機でない（simulation）、閉じている、期待したON/OFFでなければRuntimeErrorを出す。

        HTTPの成功だけでは成功と扱わない。Arduinoの応答（last_acknowledged_running）まで確かめる。
        """
        if tool not in {'start_conveyor', 'stop_conveyor', 'get_conveyor_status'}:
            raise ValueError('許可されていない操作')
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': tool, 'arguments': {}}}
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers={
            'Authorization': 'Bearer ' + self.token,
            'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
            'MCP-Protocol-Version': '2025-11-25'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            envelope = json.load(response)
        if 'error' in envelope:
            raise RuntimeError('MCP RPCエラー')
        result = envelope['result']
        if result.get('isError'):
            raise RuntimeError('MCP操作が失敗しました')
        state = result.get('structuredContent')
        if state is None:
            texts = [item['text'] for item in result.get('content', []) if item.get('type') == 'text']
            if len(texts) != 1:
                raise RuntimeError('MCP応答形式が不正')
            state = json.loads(texts[0])
        if state.get('simulation') is not False or state.get('closed') is not False:
            raise RuntimeError('実機MCPの指令応答を確認できません')
        # 状態の読み取りは、ON/OFFのどちらでも正しい応答なので、期待値と比べない
        if tool == 'get_conveyor_status':
            return state
        expected = tool == 'start_conveyor'
        if (state.get('last_acknowledged_running') is not expected
                or (expected and state.get('fault') is not None)):
            raise RuntimeError('実機MCPの指令応答を確認できません')
        return state

    def start(self):
        """ベルトを回し、ONの応答を確かめる。"""
        return self.call('start_conveyor')

    def stop(self):
        """ベルトを止め、OFFの応答を確かめる。"""
        return self.call('stop_conveyor')

    def running(self):
        """conveyor-mcpの最後の指令応答をTrue（ON）/False（OFF）で返す。分からなければNone。

        指令は送らない。Arduinoが受け付けた指令の記録で、ベルトの動きを測った値ではない。
        MCPに届かない、実機でない（simulation）、閉じているときは例外を出す。
        """
        # 撮影のたびに呼ぶので、MCPが遅くても撮影を長く止めない短い時間にする
        state = self.call('get_conveyor_status', timeout=2)
        value = state.get('last_acknowledged_running')
        return value if isinstance(value, bool) else None
