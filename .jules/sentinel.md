## 2025-01-01 - Prevent Sensitive Information Exposure via getpass
**Vulnerability:** The `input()` function was being used to read the API key from the command line in `uc.py`, making it vulnerable to shoulder surfing and potentially recording the key in terminal history.
**Learning:** Using `input()` for sensitive information like API keys exposes the data on the screen.
**Prevention:** Always use `getpass.getpass()` for sensitive command line inputs to mask the typed characters and prevent terminal history logging.
