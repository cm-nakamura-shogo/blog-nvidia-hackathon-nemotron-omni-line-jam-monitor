import sys
from seqdiag import draw
cloud, edge = sys.argv[1], sys.argv[2]
P = ['monitor.py', 'VLM']
MON, VLM = P
draw(P, [
    ('group', 'loop: 0.5秒ごとに撮影', [
        ('msg', MON, VLM, '① ROIの障害物は？'),
        ('msg', MON, VLM, '② 全景のサムズアップは？（①と並列）'),
        ('reply', VLM, MON, '① clear / duck_blocked'),
        ('reply', VLM, MON, '② none / thumbs_up'),
    ]),
    ('note', MON, 'duck_blocked が3回連続で停止'),
], cloud, title='クラウド版: 撮影のたびに2つの問いを並列に送る', col_width=5.2)
draw(P, [
    ('group', 'loop: 応答が返るたびに、最新の画像で次を聞く', [
        ('group', 'alt: 通常（警報なし）', [
            ('msg', MON, VLM, 'ROIの障害物は？'),
            ('reply', VLM, MON, 'clear / duck_blocked'),
            ('note', MON, 'duck_blocked なら停止して警報中へ'),
        ]),
        ('group', 'alt: 警報中', [
            ('msg', MON, VLM, 'ROIのサムズアップは？'),
            ('reply', VLM, MON, 'none / thumbs_up'),
            ('note', MON, 'thumbs_up なら再開して通常へ'),
        ]),
    ]),
], edge, title='エッジ版: 状態に応じて1つの問いだけを送る', col_width=5.2)
