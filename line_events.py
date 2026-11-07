"""手前のベルトの詰まりを、観察の続き方で確定する。転落や持ち去りは検出しない。"""
from datetime import datetime


def detect_line_event(records, jam_seconds=4, max_gap=6, jam_count=3):
    """直近の途切れていない観察だけを見て、詰まりなら警報の結果を返す。詰まりでなければNone。

    欠けた観察をまたいで数えない。ベルトがONの間のduck_blockedが、最新からjam_count回続けば詰まり。
    jam_countが2以上なら、最初と最後の撮影時刻の差がjam_seconds以上であることも求める。
    1なら1回で確定する（AGX OrinのVLMは1回に約4秒かかり、回数を重ねると止まるまでが長い）。
    """
    suffix = []
    for record in records:
        obs = record.get('observation')
        stamp = datetime.fromisoformat(record['captured_at']).timestamp()
        if suffix and not 0 < stamp - suffix[-1][0] <= max_gap:
            suffix = []
        if not obs or record.get('valid') is False:
            suffix = []
            continue
        suffix.append((stamp, record))

    blocked = []
    for stamp, row in reversed(suffix):
        obs = row['observation']
        if row.get('foreground_command') != 'on' or obs.get('detail_blockage', {}).get('blockage') != 'duck_blocked':
            break
        blocked.append((stamp, row))
    if len(blocked) >= jam_count and (jam_count == 1 or blocked[0][0] - blocked[-1][0] >= jam_seconds):
        return dict(status='abnormal', event='jam', location='foreground',
                    reason='手前ベルトへON指令中、障害物がアヒルの進路を塞いでいます' if jam_count == 1 else '手前ベルトへON指令中、障害物がアヒルの進路を塞いだ状態が継続しています',
                    evidence=f"{blocked[-1][1]['captured_at']}〜{blocked[0][1]['captured_at']}の{len(blocked)}観察。ONは指令受付であり実動作の測定ではありません")
    return None


def pending_obstruction(records):
    """詰まりの候補がまだ確定していない間は、正常ではなく「確認中」（unknown）を返す。候補がなければNone。"""
    recent = list(records)[-3:]
    if any(row.get('foreground_command') == 'on' and
           (row.get('observation') or {}).get('detail_blockage', {}).get('blockage') == 'duck_blocked'
           for row in recent):
        return dict(status='unknown', event='uncertain', location='foreground',
                    reason='詰まり候補を観察中。継続または解除を確認しています',
                    evidence='直近の拡大観察に進路の閉塞があるため、正常とはまだ確定していません')
    return None
