## 2024-05-17 - Hardcoded API Key Placeholder Fix

**Vulnerability:** A hardcoded placeholder string (`API_KEY = "PASTE_API_KEY_HERE"`) was used in `uc.py` along with `input()` to prompt users for missing keys, exposing sensitive data to shoulder surfing and structural encouragement to hardcode secrets.
**Learning:** Hardcoded placeholders encourage bad security practices, and `input()` leaves secrets visible in terminal history and onscreen.
**Prevention:** Avoid hardcoded placeholders. Rely on standard secure config retrieval mechanisms such as environment variables (`os.environ.get`) and configuration files, prioritizing environment variables. Always use `getpass.getpass()` for interactive prompting of sensitive information like API keys.
