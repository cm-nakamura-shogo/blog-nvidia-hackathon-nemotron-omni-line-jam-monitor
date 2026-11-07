from copy import deepcopy
import pytest
from line_events import detect_line_event, pending_obstruction


def rows():
    return [dict(captured_at=f'2026-10-23T10:00:{i*2:02d}+00:00', valid=True,
                 foreground_command='on', observation={'detail_blockage':{'blockage':'duck_blocked'}})
            for i in range(3)]


def test_three_observations_and_duration_required():
    assert detect_line_event(rows())['event']=='jam'
    assert detect_line_event(rows()[:2]) is None
    assert detect_line_event(rows(), jam_seconds=5) is None


@pytest.mark.parametrize('kind',['off','clear','uncertain','invalid','gap','duplicate'])
def test_no_jam_across_unconfirmed_measurements(kind):
    history=rows()
    if kind=='off':history[-1]['foreground_command']='off'
    if kind in ('clear','uncertain'):history[1]['observation']['detail_blockage']['blockage']=kind
    if kind=='invalid':history[1]['valid']=False
    if kind=='gap':history[-1]['captured_at']='2026-10-23T10:00:30+00:00'
    if kind=='duplicate':history[-1]['captured_at']=history[-2]['captured_at']
    assert detect_line_event(history) is None


def test_old_fall_evidence_does_not_trigger_alarm():
    history=rows()
    for i,row in enumerate(history):
        row['observation']['detail_blockage']={'blockage':'clear'}
        row['observation']['detail_ducks']=[{'x':.4,'y':.5,'surface':'green_belt' if i==0 else 'white_table'}]
    assert detect_line_event(history) is None


def test_pending_clears_after_three_clear_frames():
    history=rows()
    assert pending_obstruction(history)['status']=='unknown'
    for row in history:row['observation']['detail_blockage']['blockage']='clear'
    assert pending_obstruction(history) is None


def test_one_answer_is_enough_when_jam_count_is_one():
    history=rows()[:1]
    assert detect_line_event(history) is None, 'the default of the function is still three'
    assert detect_line_event(history, jam_count=1)['event']=='jam'
    assert detect_line_event(history, jam_seconds=30, jam_count=1)['event']=='jam', 'duration is not used for one answer'
    history[0]['foreground_command']='off'
    assert detect_line_event(history, jam_count=1) is None, 'a stopped belt is never a jam'
    history=rows();history[-1]['observation']['detail_blockage']['blockage']='clear'
    assert detect_line_event(history, jam_count=1) is None, 'only the newest answer counts'
