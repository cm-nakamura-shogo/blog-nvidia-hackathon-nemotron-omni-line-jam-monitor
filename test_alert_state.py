import pytest
from alert_state import AlertState

BAD = {'status': 'abnormal', 'event': 'jam'}
NONE = {'operator_gesture': 'none', 'human_hand': 'none'}
GOOD = {'operator_gesture': 'thumbs_up', 'human_hand': 'near'}


def test_alarm_requires_new_two_frame_gesture_and_confirmed_ack(tmp_path):
    path = tmp_path / 'state.json'
    state = AlertState(path)
    state.latch(BAD, 'time')
    assert not state.observe_gesture(GOOD, 1)
    assert not state.observe_gesture(GOOD, 3)
    assert not state.observe_gesture(NONE, 5)
    assert not state.observe_gesture(GOOD, 7)
    assert not state.observe_gesture(GOOD, 7)  # same frame
    assert state.observe_gesture(GOOD, 9)
    assert state.incident  # caller must confirm ON before clearing
    state.acknowledge()
    assert state.incident is None
    assert AlertState(path).incident is None


def test_fault_and_uncertainty_break_confirmation():
    state = AlertState()
    state.latch(BAD, 'time')
    state.observe_gesture(NONE, 1)
    state.observe_gesture(GOOD, 3)
    assert not state.observe_gesture({**GOOD, 'operator_gesture': 'uncertain'}, 5)
    assert not state.observe_gesture(GOOD, 7)
    state.invalidate()
    assert not state.observe_gesture(GOOD, 9)
    state.observe_gesture(NONE, 11)
    state.observe_gesture(GOOD, 13)
    assert not state.observe_gesture(GOOD, 30)  # gap
    assert state.incident


def test_restart_keeps_alarm_not_gesture_progress(tmp_path):
    path = tmp_path / 'state.json'
    state = AlertState(path)
    state.latch(BAD, 'time')
    state.observe_gesture(NONE, 1)
    state.observe_gesture(GOOD, 3)
    restarted = AlertState(path)
    assert restarted.incident
    assert not restarted.observe_gesture(GOOD, 5)
    restarted.latch({'status': 'normal'}, 'later')
    assert restarted.incident


def test_initial_start_wait_and_alarm_priority(tmp_path):
    state = AlertState(tmp_path / 'state.json')
    state.wait_for_start()
    assert state.waiting_start
    assert not state.observe_gesture(GOOD, 1)
    assert state.observe_gesture(GOOD, 3)
    assert state.waiting_start  # cleared only after ON acknowledgement
    state.latch(BAD, 'now')
    assert not state.waiting_start
    state.wait_for_start()
    assert state.incident['result'] == BAD  # startup cannot erase a real alarm
    assert not state.observe_gesture(GOOD, 5)


@pytest.mark.parametrize('hand', ['near', 'touching_belt', 'holding_yellow_object', 'none', 'uncertain'])
@pytest.mark.parametrize('startup', [True, False])
def test_gesture_confirmation_is_independent_of_contact(hand, startup):
    state = AlertState()
    if startup:
        state.wait_for_start()
    else:
        state.latch(BAD, 'time')
        state.observe_gesture(NONE, 1)
    gesture = {'operator_gesture': 'thumbs_up', 'human_hand': hand}
    assert not state.observe_gesture(gesture, 3)
    assert state.count == 1
    assert state.observe_gesture(gesture, 5)
    assert state.incident is not None  # ON acknowledgement still required

@pytest.mark.parametrize('event', ['human_removal', 'count_loss', 'fall', 'jam', 'monitoring_error'])
def test_old_removed_alarm_migration_keeps_thumbs_up_start(tmp_path, event):
    path = tmp_path / 'state.json'
    state = AlertState(path)
    state.latch({'status': 'abnormal', 'event': event}, 'time')
    state = AlertState(path)
    state.retire_removed_events()
    state.wait_for_start()
    assert state.waiting_start == (event in {'human_removal', 'count_loss', 'fall'})
    assert state.incident is not None
    if event not in {'human_removal', 'count_loss', 'fall'}:
        assert state.incident['result']['event'] == event


def test_one_thumbs_up_after_a_fresh_none_clears_when_required_is_one():
    state = AlertState(thumbs_required=1)
    state.latch(BAD, 't')
    assert not state.observe_gesture(GOOD, 1), 'a thumbs-up already showing at the alarm does not count'
    assert not state.observe_gesture(NONE, 3)
    assert state.observe_gesture(GOOD, 5)
