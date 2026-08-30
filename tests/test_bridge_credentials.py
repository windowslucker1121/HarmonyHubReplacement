"""`bridge.credentials`: the MQTT broker password, kept out of `hub_settings.json`.

Same shape as the Home Assistant *backend*'s token storage, for the same
reason -- see `bridge/credentials.py`'s module docstring.
"""

from __future__ import annotations

from harmony_hub.bridge import credentials


def test_no_password_reads_as_empty_string(tmp_path):
    assert credentials.read_password("hub1", tmp_path) == ""


def test_round_trips_a_password(tmp_path):
    credentials.write_password("hub1", "s3cret", tmp_path)
    assert credentials.read_password("hub1", tmp_path) == "s3cret"


def test_password_is_trimmed(tmp_path):
    credentials.write_password("hub1", "  s3cret  \n", tmp_path)
    assert credentials.read_password("hub1", tmp_path) == "s3cret"


def test_two_node_ids_do_not_share_a_file(tmp_path):
    credentials.write_password("hub1", "one", tmp_path)
    credentials.write_password("hub2", "two", tmp_path)
    assert credentials.read_password("hub1", tmp_path) == "one"
    assert credentials.read_password("hub2", tmp_path) == "two"


def test_clear_password_removes_it(tmp_path):
    credentials.write_password("hub1", "s3cret", tmp_path)
    credentials.clear_password("hub1", tmp_path)
    assert credentials.read_password("hub1", tmp_path) == ""


def test_clear_password_is_safe_when_nothing_was_ever_written(tmp_path):
    credentials.clear_password("hub1", tmp_path)  # must not raise
