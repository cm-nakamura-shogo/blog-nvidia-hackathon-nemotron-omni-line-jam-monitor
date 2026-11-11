import sys
from seqdiag import draw
out = sys.argv[1]
P = ['camera_preview.py', 'monitor.py', 'VLM\n（Jetson AGX Orin）', 'コンベアの\nMCPサーバー', '人']
CAM, MON, VLM, MCP, HUM = P
draw(P, [
    ('group', '通常（警報なし）: 応答が返るたびに繰り返す', [
        ('msg', MON, CAM, '最新の1枚を取得'),
        ('reply', CAM, MON, '画像'),
        ('msg', MON, MCP, 'get_conveyor_status'),
        ('reply', MCP, MON, '動作中'),
        ('msg', MON, VLM, 'ROIの障害物は？'),
        ('reply', VLM, MON, 'duck_blocked'),
    ]),
    ('msg', MON, MCP, 'stop_conveyor'),
    ('reply', MCP, MON, 'OFF'),
    ('note', MON, '警報を保持'),
    ('note', HUM, '詰まりを取り除き\nサムズアップをする'),
    ('group', '警報中: サムズアップを待つ', [
        ('msg', MON, CAM, '最新の1枚を取得'),
        ('reply', CAM, MON, '画像'),
        ('msg', MON, VLM, 'ROIのサムズアップは？'),
        ('reply', VLM, MON, 'thumbs_up'),
    ]),
    ('msg', MON, MCP, 'start_conveyor'),
    ('reply', MCP, MON, 'ON'),
    ('note', MON, '警報を解除'),
], out)
