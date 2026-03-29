import sys
from unittest.mock import MagicMock

# The environment is network-restricted, so we mock requests globally
# to prevent tests from failing to import modules that depend on it
sys.modules['requests'] = MagicMock()
sys.modules['requests.exceptions'] = MagicMock()
