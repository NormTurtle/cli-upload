import os
import sys
import pytest
from unittest.mock import patch

# Mock requests module so uc.py can be imported without internet access
import sys
from unittest.mock import MagicMock
sys.modules['requests'] = MagicMock()
sys.modules['requests.adapters'] = MagicMock()

import uc

def test_prompt_key_uses_getpass():
    with patch('getpass.getpass') as mock_getpass:
        mock_getpass.return_value = "my_secret_key"
        key = uc.prompt_key()
        assert key == "my_secret_key"
        mock_getpass.assert_called_once_with("UC Files API Key (32-char hash): ")

def test_prompt_key_exits_on_empty():
    with patch('getpass.getpass') as mock_getpass, patch('sys.exit') as mock_exit:
        mock_getpass.return_value = "   "
        uc.prompt_key()
        mock_exit.assert_called_once_with(1)
