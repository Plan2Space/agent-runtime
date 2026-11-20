"""Durable state file: atomic write and tolerant read (issue #20, FP-062).

Regression first: a zeroed or corrupt state file must not crash the loader silently, and the
bad bytes must be preserved (quarantined, never deleted). A missing file must never be taken
as a first run unless the caller says so, because that would reset dedup and budget state.
"""
import json
import os
from pathlib import Path

import pytest

from agent_runtime import state_file as sf

INCIDENT_BYTES = 20_888  # the size measured in the historical incident (#633): all zero bytes


def test_zeroed_state_file_is_quarantined_not_silently_reset(tmp_path):
    p = tmp_path / "event_bridge.json"
    p.write_bytes(b"\x00" * INCIDENT_BYTES)
    with pytest.raises(sf.StateCorrupt, match="quarantined"):
        sf.load_state(p, default={"queue": []})
    assert not p.exists()  # moved aside, so the next start does not read the bad file again
    (saved,) = tmp_path.glob("event_bridge.json.corrupt-*")
    assert saved.read_bytes() == b"\x00" * INCIDENT_BYTES  # evidence preserved byte for byte


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


def test_utf8_bom_is_treated_as_corrupt_by_policy(tmp_path):
    # write_atomic never writes a BOM, so one means the file was written by something else.
    p = tmp_path / "state.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"queue": []}).encode("utf-8"))
    with pytest.raises(sf.StateCorrupt):
        sf.load_state(p)


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


def test_quarantine_name_collision_never_overwrites_evidence(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    p.write_bytes(b"first bad")
    monkeypatch.setattr(sf.secrets, "token_hex", lambda n: "abcdef")  # force the same name every time
    with pytest.raises(sf.StateCorrupt):
        sf.load_state(p, default={})
    p.write_bytes(b"second bad")
    with pytest.raises(sf.StateCorrupt, match="not quarantined"):
        sf.load_state(p, default={})
    (saved,) = tmp_path.glob("state.json.corrupt-*")
    assert saved.read_bytes() == b"first bad"  # the first evidence is untouched
    assert p.read_bytes() == b"second bad"  # the second file stays in place


def test_quarantine_failure_still_raises_state_corrupt_with_the_cause(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    p.write_bytes(b"")

    def denied(path):
        raise PermissionError("arquivo aberto por outro processo")
    monkeypatch.setattr(sf, "_quarantine", denied)
    with pytest.raises(sf.StateCorrupt, match="left in place") as info:
        sf.load_state(p, default={})
    assert isinstance(info.value.__cause__, ValueError)  # the original corruption is chained
    assert p.exists()  # nothing was moved, nothing was deleted


def test_missing_file_without_default_is_an_error_not_a_first_run(tmp_path):
    with pytest.raises(sf.StateMissing):
        sf.load_state(tmp_path / "state.json")


def test_missing_file_with_default_returns_a_copy_of_the_default(tmp_path):
    default = {"queue": []}
    loaded = sf.load_state(tmp_path / "state.json", default=default)
    assert loaded == {"queue": []}
    loaded["queue"].append("x")
    assert default == {"queue": []}  # the caller's default is not mutated


def test_missing_parent_directory_is_missing_even_with_a_default(tmp_path):
    # A volume that is not mounted must not become an empty state for the next cycle.
    with pytest.raises(sf.StateMissing):
        sf.load_state(tmp_path / "volume_desmontado" / "state.json", default={"queue": []})


def test_round_trip_through_atomic_write(tmp_path):
    p = tmp_path / "state.json"
    data = {"queue": [{"key": "k1", "state": "QUEUED", "attempts": 0}], "seen": ["r1"]}
    sf.write_atomic(p, data)
    assert sf.load_state(p) == data
    assert json.loads(p.read_text(encoding="utf-8")) == data


def test_round_trip_keeps_non_ascii_text(tmp_path):
    p = tmp_path / "state.json"
    data = {"nota": "revisão ✔ 日本"}
    sf.write_atomic(p, data)
    assert sf.load_state(p) == data


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


def test_failed_fsync_keeps_the_old_file_and_removes_the_temp_file(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    sf.write_atomic(p, {"queue": [{"key": "old"}]})
    before = p.read_bytes()

    def disk_full(fd):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "fsync", disk_full)
    with pytest.raises(OSError, match="No space"):
        sf.write_atomic(p, {"queue": [{"key": "new"}]})
    assert p.read_bytes() == before
    assert sorted(f.name for f in tmp_path.iterdir()) == ["state.json"]


def test_cleanup_failure_does_not_hide_the_original_error(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    sf.write_atomic(p, {"queue": []})

    def replace_fails(src, dst):
        raise OSError("ORIGINAL: replace")

    def unlink_locked(self, missing_ok=False):
        raise PermissionError("tmp travado por antivirus")
    monkeypatch.setattr(os, "replace", replace_fails)
    monkeypatch.setattr(Path, "unlink", unlink_locked)
    with pytest.raises(OSError, match="ORIGINAL: replace"):
        sf.write_atomic(p, {"queue": [{"key": "new"}]})


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
    assert Path(src).parent == tmp_path  # same filesystem, so rename is atomic
