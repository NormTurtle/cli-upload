import os
import sys
from unittest.mock import patch, MagicMock
from pathlib import Path

# Mock requests because of network restriction mentioned in memory
import types
if 'requests' not in sys.modules:
    mock_requests = types.ModuleType('requests')

    # Create an adapters module
    adapters = types.ModuleType('requests.adapters')
    adapters.HTTPAdapter = MagicMock
    mock_requests.adapters = adapters
    sys.modules['requests.adapters'] = adapters

    # Set main attributes
    mock_requests.Session = MagicMock
    mock_requests.get = MagicMock
    mock_requests.post = MagicMock
    mock_requests.put = MagicMock
    sys.modules['requests'] = mock_requests

import uc

def test_load_key_override():
    # Test override flag has highest priority
    with patch.dict(os.environ, {"UC_API_KEY": "env_key"}):
        key = uc.load_key("override_key")
        assert key == "override_key"

def test_load_key_env_var():
    # Test env var has priority over file
    with patch.dict(os.environ, {"UC_API_KEY": "env_key"}):
        with patch("uc.Path.exists", return_value=True):
            with patch("uc.Path.read_text", return_value="file_key"):
                key = uc.load_key(None)
                assert key == "env_key"

def test_load_key_file():
    # Test file is used if no env var or override
    with patch.dict(os.environ, {}, clear=True):
        with patch("uc.Path.exists", return_value=True):
            with patch("uc.Path.read_text", return_value="file_key"):
                key = uc.load_key(None)
                assert key == "file_key"

def test_load_key_empty():
    # Test returns empty string if nothing found
    with patch.dict(os.environ, {}, clear=True):
        with patch("uc.Path.exists", return_value=False):
            key = uc.load_key(None)
            assert key == ""

@patch("uc.getpass.getpass")
def test_prompt_key(mock_getpass):
    # Test getpass is called and sys.exit is not
    mock_getpass.return_value = "my_secret_key"
    with patch("sys.exit") as mock_exit:
        key = uc.prompt_key()
        assert key == "my_secret_key"
        mock_getpass.assert_called_once()
        mock_exit.assert_not_called()

@patch("uc.getpass.getpass")
def test_prompt_key_empty(mock_getpass):
    # Test sys.exit is called if key is empty
    mock_getpass.return_value = ""
    with patch("sys.exit") as mock_exit:
        uc.prompt_key()
        mock_exit.assert_called_once_with(1)
