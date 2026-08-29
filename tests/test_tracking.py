"""PressTracker against synthetic timelines, driven by a fake clock.

The release timeout is a timing rule, so these tests move time by hand
rather than sleeping.
"""

from __future__ import annotations

import pytest

from harmony_receiver.protocol import parse_frame
from harmony_receiver.tracking import PressTracker

VOLUME_UP = bytes.fromhex("17C3E90000000000003D")  # consumer 0x00E9
CONSUMER_RELEASE = bytes.fromhex("00C3000000000000003D")
UP_ARROW = bytes.fromhex("17C100520000000000D6")  # keyboard 0x52
DOWN_ARROW = bytes.fromhex("00C100510000000000EE")  # keyboard 0x51
KEYBOARD_RELEASE = bytes.fromhex("00C1000000000000003F")
STATUS = bytes.fromhex("004F00044C0000000061")
TICK = bytes.fromhex("0040044C70")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


def feed(tracker, raw):
    return list(tracker.feed(parse_frame(raw)))


def test_a_report_emits_a_press(clock):
    tracker = PressTracker(clock=clock)

    events = feed(tracker, VOLUME_UP)

    assert [e.kind for e in events] == ["press"]
    assert events[0].name == "Volume Up"
    assert events[0].usage == 0x00E9


def test_the_release_report_emits_a_release(clock):
    tracker = PressTracker(clock=clock)
    feed(tracker, VOLUME_UP)

    events = feed(tracker, CONSUMER_RELEASE)

    assert [e.kind for e in events] == ["release"]
    assert events[0].name == "Volume Up"  # names the button that was let go


def test_the_same_report_repeated_is_one_press(clock):
    """The remote sends its report to two addresses; that is still one press."""
    tracker = PressTracker(clock=clock)

    first = feed(tracker, VOLUME_UP)
    clock.advance(0.01)
    second = feed(tracker, bytes.fromhex("00C3E900000000000054"))  # session copy

    assert [e.kind for e in first] == ["press"]
    assert second == []


def test_ticks_while_held_emit_repeats(clock):
    tracker = PressTracker(clock=clock)
    feed(tracker, VOLUME_UP)

    repeats = []
    for _ in range(3):
        clock.advance(0.1)
        assert list(tracker.tick()) == []  # still held
        repeats += feed(tracker, TICK)

    assert [e.kind for e in repeats] == ["repeat"] * 3
    assert all(e.name == "Volume Up" for e in repeats)


def test_a_lost_release_still_ends_the_press(clock):
    """Packets do get dropped; a button must never stay stuck down."""
    tracker = PressTracker(clock=clock)
    feed(tracker, VOLUME_UP)

    clock.advance(0.2)
    assert list(tracker.tick()) == []

    clock.advance(0.2)
    events = list(tracker.tick())

    assert [e.kind for e in events] == ["release"]
    assert tracker.active_signature is None


def test_release_fires_only_once(clock):
    tracker = PressTracker(clock=clock)
    feed(tracker, VOLUME_UP)

    assert len(feed(tracker, CONSUMER_RELEASE)) == 1
    clock.advance(5.0)
    assert list(tracker.tick()) == []
    assert feed(tracker, CONSUMER_RELEASE) == []


def test_a_different_button_releases_the_previous_one(clock):
    """A press whose release went missing must not swallow the next button."""
    tracker = PressTracker(clock=clock)
    feed(tracker, UP_ARROW)
    clock.advance(0.05)

    events = feed(tracker, DOWN_ARROW)

    assert [e.kind for e in events] == ["release", "press"]
    assert events[0].name == "Up Arrow"
    assert events[1].name == "Down Arrow"


def test_same_usage_on_a_different_page_is_a_different_button(clock):
    """Keyboard 0x52 and a consumer 0x52 are unrelated keys."""
    tracker = PressTracker(clock=clock)
    feed(tracker, UP_ARROW)  # keyboard 0x52

    events = feed(tracker, bytes.fromhex("00C352000000000000EB"))  # consumer 0x52

    assert [e.kind for e in events] == ["release", "press"]


def test_keyboard_release_ends_a_keyboard_press(clock):
    tracker = PressTracker(clock=clock)
    feed(tracker, UP_ARROW)

    assert [e.kind for e in feed(tracker, KEYBOARD_RELEASE)] == ["release"]


def test_status_frames_are_ignored(clock):
    """0x4F traffic arrives constantly and must not look like a button."""
    tracker = PressTracker(clock=clock)

    assert feed(tracker, STATUS) == []
    assert tracker.active_signature is None


def test_status_frames_do_not_keep_a_press_alive(clock):
    """Status traffic continues after a release, so it must not stall the timeout."""
    tracker = PressTracker(clock=clock)
    feed(tracker, VOLUME_UP)

    clock.advance(0.3)
    feed(tracker, STATUS)
    clock.advance(0.1)

    assert [e.kind for e in tracker.tick()] == ["release"]


def test_ticks_with_nothing_held_are_ignored(clock):
    """The remote keeps ticking for ~30s after a release; that is not a button."""
    tracker = PressTracker(clock=clock)

    assert feed(tracker, TICK) == []
    assert list(tracker.tick()) == []


def test_a_learned_label_overrides_the_hid_name(clock):
    """The operator naming a button on this remote beats a generic usage name."""
    tracker = PressTracker(resolve_label=lambda s: "TV Louder" if s == "C3E90000" else None, clock=clock)

    assert feed(tracker, VOLUME_UP)[0].name == "TV Louder"


def test_an_unknown_usage_still_reports_something_identifying(clock):
    tracker = PressTracker(clock=clock)
    payload = bytes([0x00, 0xC3, 0x77, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00, 0xBF])

    event = feed(tracker, payload)[0]

    assert event.usage == 0x0777
    assert "0x777" in event.name.lower() or "777" in event.name
