import pytest
from viking import human_size, extract_public_share_token, should_skip, _load_resume_state, _save_resume_state
import json
import os
from pathlib import Path

def test_human_size():
    assert human_size(0) == "0.00 B"
    assert human_size(1023) == "1023.00 B"
    assert human_size(1024) == "1.00 KB"
    assert human_size(1024 * 1024 * 1.5) == "1.50 MB"
    assert human_size(1024**5 * 2) == "2.00 PB"
    assert human_size(1024**6 * 3) == "3.00 EB" # Fallback

def test_extract_public_share_token():
    assert extract_public_share_token("") == ""
    assert extract_public_share_token(None) == ""
    assert extract_public_share_token("token123") == "token123"
    assert extract_public_share_token("https://vikingfile.com/public-upload/token123") == "token123"
    assert extract_public_share_token("https://vikingfile.com/public-upload/token123/") == "token123"

def test_should_skip():
    assert should_skip(".viking_logs") is True
    assert should_skip("/path/to/.viking_logs") is True
    assert should_skip("some_file.viking_session_123") is True
    assert should_skip("normal_file.txt") is False

def test_resume_state(tmp_path):
    state_file = tmp_path / "state.json"

    # Test loading non-existent
    state = _load_resume_state(state_file)
    assert state == {"version": 1, "done": {}}

    # Test saving and loading
    new_state = {"version": 1, "done": {"file1.txt": {"size": 100, "mtime_ns": 12345}}}
    _save_resume_state(state_file, new_state)

    loaded = _load_resume_state(state_file)
    assert loaded == new_state

    # Test corrupted file
    state_file.write_text("invalid json")
    corrupted_load = _load_resume_state(state_file)
    assert corrupted_load == {"version": 1, "done": {}}
