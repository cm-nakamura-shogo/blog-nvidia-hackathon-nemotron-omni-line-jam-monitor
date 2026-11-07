import io,json
import pytest
from PIL import Image
from detail_observation import make_detail_crop,inspect_blockage


def test_optional_roi_preserves_full_frame():
    buf=io.BytesIO();Image.new('RGB',(640,400)).save(buf,format='JPEG')
    jpeg=buf.getvalue()
    assert make_detail_crop(jpeg,None)==jpeg
    assert Image.open(io.BytesIO(make_detail_crop(jpeg,(.2,.3,.5,.9)))).size==(310,520)


@pytest.mark.parametrize('result',[{}, {'blockage':'fall','reason':'no'}, {'blockage':'clear','reason':True}])
def test_invalid_blockage_rejected(monkeypatch,result):
    monkeypatch.setattr('detail_observation.call_model',lambda *a,**k:(json.dumps(result),None))
    with pytest.raises(ValueError):inspect_blockage('unused','model',b'jpeg')


@pytest.mark.parametrize('lang,japanese',[('en',False),('ja',True)])
def test_reason_language_changes_only_the_reason_request(monkeypatch,lang,japanese):
    sent=[]
    def call(url,model,content,schema):
        sent.append(content[0]['text']);return json.dumps({'blockage':'clear','reason':'アヒルはいない' if japanese else 'no duck'}),None
    monkeypatch.setattr('detail_observation.call_model',call)
    result,_,_=inspect_blockage('unused','model',b'jpeg',reason_lang=lang)
    assert result['blockage']=='clear'
    assert ('in Japanese' in sent[0])==japanese
    assert sent[0].startswith('Look at this conveyor close-up.'), 'the judgement part of the prompt is unchanged'
