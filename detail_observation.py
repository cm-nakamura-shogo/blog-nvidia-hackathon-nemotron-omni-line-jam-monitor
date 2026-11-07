"""画像1枚のROIを切り出し、ベルトの障害物をVLMに聞く。"""
import base64
import io
import json
from PIL import Image
from observation_pipeline import call_model

def make_detail_crop(jpeg, roi):
    """ROIを切り出し、310×520のJPEGにして返す。roiがNoneなら元の画像のまま返す。"""
    if roi is None:
        return jpeg
    with Image.open(io.BytesIO(jpeg)) as source:
        width, height = source.size
        crop = source.crop(tuple(round(value * (width if i % 2 == 0 else height)) for i, value in enumerate(roi)))
        crop = crop.resize((310, 520))
        output = io.BytesIO()
        crop.save(output, format='JPEG', quality=90)
    return output.getvalue()


BLOCKAGE_PROMPT='''Look at this conveyor close-up. Is there a physical barrier blocking the path of a yellow rubber duck traveling along the green belt towards the bottom of the image? A finger or palm across the belt in front of a duck, between the duck and the bottom of the image, counts as a barrier even if the duck has not reached it yet. A hand above the belt, holding a duck, or merely beside the belt is NOT a barrier. Ignore ducks on the white table. If there is no duck on the belt, report clear. Report duck_blocked if a barrier is across the belt ahead of a duck on the belt. Return {"blockage":"clear|duck_blocked|uncertain", "reason":"one short sentence describing visible evidence"}.'''
BLOCKAGE_SCHEMA={'type':'object','additionalProperties':False,'properties':{'blockage':{'type':'string','enum':['clear','duck_blocked','uncertain']},'reason':{'type':'string','maxLength':180}},'required':['blockage','reason']}


# 理由の言語だけを変える。判定の値と指示のほかの部分は変えず、精度への影響を小さくする
REASON_JA = ' Write the reason in Japanese, in one short sentence of at most 60 characters.'


def inspect_blockage(url, model, jpeg, full_scene=False, reason_lang='en'):
    """画像1枚の障害物をVLMに聞き、(結果, 生の返事, usage)を返す。

    結果は {'blockage': clear|duck_blocked|uncertain, 'reason': 文}。形が違えばValueErrorを出す。
    full_sceneは、ROIなしで全景を送るときの指示に切り替える。reason_langは'en'か'ja'。
    """
    prompt = (('Inspect only the near/foreground part of the long LEFT green belt, not the upstream belt or right branch. ' + BLOCKAGE_PROMPT.replace('Look at this conveyor close-up.', 'Look at this full scene.')) if full_scene else BLOCKAGE_PROMPT)
    if reason_lang == 'ja':
        prompt += REASON_JA
    raw, usage = call_model(url, model, [
        {'type': 'text', 'text': prompt},
        {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(jpeg).decode()}}
    ], schema=BLOCKAGE_SCHEMA)
    result = json.loads(raw)
    if not isinstance(result, dict) or set(result) != {'blockage', 'reason'}:
        raise ValueError('拡大障害物JSONが不正')
    if result['blockage'] not in ('clear', 'duck_blocked', 'uncertain') or not isinstance(result['reason'], str):
        raise ValueError('拡大障害物判定が不正')
    return result, raw, usage
