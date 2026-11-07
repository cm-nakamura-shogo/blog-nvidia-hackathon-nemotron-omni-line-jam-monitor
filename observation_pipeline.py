"""画像1枚の👍の観察と、VLMへの要求（障害物の観察と共通）。"""
import base64
import json
import urllib.request

OBSERVATION_PROMPT = """Inspect this image for a HUMAN thumbs-up gesture.
Return operator_gesture: thumbs_up, none, or uncertain.
thumbs_up requires an extended upward thumb and other fingers curled, holding no object.
A robot gripper, pointing finger, open palm or hand holding a duck is NOT thumbs_up.
Judge independently of proximity to equipment. Return only JSON."""
OBSERVATION_SCHEMA = {'type':'object','additionalProperties':False,
    'properties':{'operator_gesture':{'type':'string','enum':['thumbs_up','none','uncertain']}},
    'required':['operator_gesture']}

class IncompleteModelResponse(ValueError):
    """VLMの答えが途中で切れた（finish_reasonがstopでない）。diagnosticsに原因を調べるための中身を持つ。"""

    def __init__(self, choice, usage):
        self.diagnostics = {
            'finish_reason': choice.get('finish_reason'),
            'message': choice.get('message'),
            'usage': usage,
        }
        super().__init__(f"モデル応答が未完了 (finish_reason={choice.get('finish_reason')!r})")


def call_model(url, model, content, schema=None):
    """VLMに1回聞き、(答えの文字列, usage)を返す。答えが途中で切れたらIncompleteModelResponseを出す。

    schemaを渡すと、その形のJSONだけを返させる。同じ画像に同じ答えが返るよう、temperatureは0にする。
    考える過程（thinking）は出させない（遅くなり、JSONの外に文が混ざるため）。応答は8秒で打ち切る。
    """
    body = {'model': model, 'messages': [{'role': 'user', 'content': content}],
            'temperature': 0, 'max_tokens': 768,
            'response_format': {'type': 'json_object'},
            'chat_template_kwargs': {'enable_thinking': False}}
    if schema is not None:
        body['response_format'] = {'type': 'json_schema', 'json_schema': {
            'name': 'observation', 'strict': True, 'schema': schema}}
    request = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=8) as response:
        result = json.load(response)
    choice = result['choices'][0]
    if choice['finish_reason'] != 'stop' or not isinstance(choice['message']['content'], str):
        raise IncompleteModelResponse(choice, result.get('usage'))
    return choice['message']['content'], result.get('usage')


def validate_observation(raw):
    """👍の観察のJSONを確かめて返す。形や値が違えばValueErrorを出す。"""
    obs = json.loads(raw)
    if not isinstance(obs, dict) or set(obs) != {'operator_gesture'} or obs['operator_gesture'] not in ('thumbs_up', 'none', 'uncertain'):
        raise ValueError('ジェスチャーJSONが不正')
    return obs


def observe(url, model, jpeg):
    """画像1枚の👍をVLMに聞き、(答え, usage)を返す。"""
    # 画像は1枚だけ送る。前の画像や前の判定に、今回の答えを引きずられないようにするため
    return call_model(url, model, [
        {'type': 'text', 'text': OBSERVATION_PROMPT},
        {'type': 'image_url', 'image_url': {'url':
            'data:image/jpeg;base64,' + base64.b64encode(jpeg).decode()}}], schema=OBSERVATION_SCHEMA)
