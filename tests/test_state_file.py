"""Durable state file: atomic write and tolerant read (issue #20, FP-062).

Regression first: a zeroed or corrupt state file must not crash the loader silently, and the
bad bytes must be preserved (quarantined, never deleted). A missing file must never be taken
as a first run unless the caller says so, because that would reset dedup and budget state.
"""
import json
import os

import pytest

from agent_runtime import state_file as sf


def test_zeroed_state_file_is_quarantined_not_silently_reset(tmp_path):
    p = tmp_path / "event_bridge.json"
    p.write_bytes(b"")  # the incident: 20 KB of zero bytes
    with pytest.raises(sf.StateCorrupt, match="quarantined"):
        sf.load_state(p, default={"queue": []})
    assert not p.exists()  # moved aside, so the next start does not read the bad file again
    quarantined = list(tmp_path.glob("event_bridge.json.corrupt-*"))
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == b""  # evidence preserved, not deleted


def test_truncated_json_is_quarantined_with_its_bytes(tmp_path):
    p = tmp_path / "state.json"
    raw = b'{"queue": [{"key": "k1", "state": "QUE'
    p.write_bytes(raw)
    with pytest.raises(sf.StateCorrupt):
        sf.load_state(p, default={"queue": []})
    (saved,) = tmp_path.glob("state.json.corrupt-*")
    assert saved.read_bytes() == raw


def test_root_that_is_not_an_object_is_corrupt(tmp_path):
    p = tmp_path / "state.json"
    p.write_bytes(b"[1, 2, 3]")
    with pytest.raises(sf.StateCorrupt):
        sf.load_state(p, default={})


def test_non_utf8_bytes_are_corrupt(tmp_path):
    p = tmp_path / "state.json"
    p.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(sf.StateCorrupt):
        sf.load_state(p, default={})


def test_two_corruptions_keep_two_separate_evidence_files(tmp_path):
    p = tmp_path / "state.json"
    p.write_bytes(b"first bad")
    with pytest.raises(sf.StateCorrupt):
        sf.load_state(p, default={})
    p.write_bytes(b"second bad")
    with pytest.raises(sf.StateCorrupt):
        sf.load_state(p, default={})
    contents = sorted(f.read_bytes() for f in tmp_path.glob("state.json.corrupt-*"))
    assert contents == [b"first bad", b"second bad"]


def test_missing_file_without_default_is_an_error_not_a_first_run(tmp_path):
    with pytest.raises(sf.StateMissing):
        sf.load_state(tmp_path / "state.json")


def test_missing_file_with_default_returns_a_copy_of_the_default(tmp_path):
    default = {"queue": []}
    loaded = sf.load_state(tmp_path / "state.json", default=default)
    assert loaded == {"queue": []}
    loaded["queue"].append("x")
    assert default == {"queue": []}  # the caller's default is not mutated


def test_round_trip_through_atomic_write(tmp_path):
    p = tmp_path / "state.json"
    data = {"queue": [{"key": "k1", "state": "QUEUED", "attempts": 0}], "seen": ["r1"]}
    sf.write_atomic(p, data)
    assert sf.load_state(p) == data
    assert json.loads(p.read_text(encoding="utf-8")) == data


def test_interrupted_write_leaves_the_old_file_intact_and_no_temp_files(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    old = {"queue": [{"key": "old"}]}
    sf.write_atomic(p, old)
    before = p.read_bytes()

    def crash_before_replace(src, dst):
        raise OSError("kill -9 entre o fsync e o rename")
    monkeypatch.setattr(os, "replace", crash_before_replace)
    with pytest.raises(OSError):
        sf.write_atomic(p, {"queue": [{"key": "new"}]})
    assert p.read_bytes() == before  # the target never holds a partial write
    assert sorted(f.name for f in tmp_path.iterdir()) == ["state.json"]  # the temp file was removed


def test_write_uses_temp_file_in_the_same_directory(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append((str(src), str(dst)))
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", spy)
    sf.write_atomic(p, {"a": 1})
    (src, dst), = seen
    assert dst == str(p)
    assert sf.Path(src).parent == tmp_path  # same filesystem, so rename is atomic
