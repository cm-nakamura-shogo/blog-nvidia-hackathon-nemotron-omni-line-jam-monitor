"""保存した画像（回したあと）を、もう一度VLMに判定させる。コンベアの操作は読み込みも呼び出しもしない。"""
import argparse
import json
from pathlib import Path
from collections import deque

from detail_observation import make_detail_crop, inspect_blockage
from line_events import detect_line_event


def main():
    """runのobservations.jsonlに記録した画像の障害物を順にVLMに聞き直し、詰まりの判定を--outputに書く。"""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('run', type=Path)
    p.add_argument('--url', default='http://localhost:8001/v1/chat/completions')
    p.add_argument('--model', default='line-base')
    p.add_argument('--start', type=int, default=0)
    p.add_argument('--end', type=int, default=100000)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--detail-roi', type=float, nargs=4)
    args = p.parse_args()
    history = deque(maxlen=8)
    with args.output.open('w', buffering=1) as out:
        for line in (args.run / 'observations.jsonl').read_text().splitlines():
            source = json.loads(line)
            number = int(source['file'][6:12])
            if not args.start <= number <= args.end:
                continue
            jpeg = (args.run / source['file']).read_bytes()
            crop = make_detail_crop(jpeg, args.detail_roi)
            block, _, _ = inspect_blockage(args.url, args.model, crop, full_scene=args.detail_roi is None)
            obs = {'detail_blockage': block}
            record = {key: source[key] for key in ('file', 'captured_at', 'foreground_command')}
            record.update(observation=obs, valid=True)
            history.append(record)
            decision = detect_line_event(history)
            out.write(json.dumps(dict(record=record, decision=decision), ensure_ascii=False)+'\n')
            print(source['file'], block['blockage'], decision and decision['event'], flush=True)



if __name__ == '__main__':
    main()
