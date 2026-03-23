import os
import hashlib
import tempfile
import pytest
from pathlib import Path

# Import the functions to test
from viking import (
    human_size,
    extract_public_share_token,
    should_skip,
    _resume_state_path,
    LOG_FILE
)

def test_human_size():
    # Test bytes
    assert human_size(0) == "0.00 B"
    assert human_size(1023) == "1023.00 B"

    # Test KB
    assert human_size(1024) == "1.00 KB"
    assert human_size(1024 * 1.5) == "1.50 KB"

    # Test MB
    assert human_size(1024**2) == "1.00 MB"

    # Test GB
    assert human_size(1024**3) == "1.00 GB"

    # Test TB
    assert human_size(1024**4) == "1.00 TB"

    # Test PB
    assert human_size(1024**5) == "1.00 PB"

    # Test fallback to EB
    assert human_size(1024**6) == "1.00 EB"
    assert human_size(1024**7) == "1024.00 EB"

def test_extract_public_share_token():
    # Test empty / None
    assert extract_public_share_token("") == ""
    assert extract_public_share_token(None) == ""

    # Test raw token
    assert extract_public_share_token("F3lH1nC3TR9S9syBWbgSxmUmA1G2ord_Czvw4_pBSas") == "F3lH1nC3TR9S9syBWbgSxmUmA1G2ord_Czvw4_pBSas"

    # Test full URL with public-upload
    assert extract_public_share_token("https://vikingfile.com/public-upload/token123") == "token123"

    # Test with trailing slash
    assert extract_public_share_token("https://vikingfile.com/public-upload/token456/") == "token456"

    # Test with whitespace
    assert extract_public_share_token("  https://vikingfile.com/public-upload/token789  ") == "token789"
    assert extract_public_share_token("  raw_token_abc  ") == "raw_token_abc"

@pytest.mark.parametrize(
    "path, expected",
    [
        # Test normal files
        ("normal_file.txt", False),
        ("/path/to/normal_file.txt", False),
        # Test LOG_FILE
        (LOG_FILE, True),
        (f"/var/log/{LOG_FILE}", True),
        # Test viking session
        (".viking_session", True),
        (".viking_session_123", True),
        ("/path/to/.viking_session_abc", True),
    ],
)
def test_should_skip(path, expected):
    assert should_skip(path) == expected

def test_resume_state_path():
    folder_path = "/my/test/folder"
    target_folder_name = "test_target"

    # Calculate expected hash
    raw = f"{os.path.abspath(folder_path)}|{target_folder_name}"
    expected_key = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    expected_path = Path(tempfile.gettempdir()) / f".viking_resume_{expected_key}.json"

    # Test without RESUME_DIR set
    # Using tempfile.gettempdir()
    path = _resume_state_path(folder_path, target_folder_name)
    assert path == expected_path
