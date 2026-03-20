## 2023-10-27 - Masking sensitive terminal inputs
**Vulnerability:** The application prompted users for API keys using the built-in `input()` function in `uc.py`, which echoes the text as it is typed, exposing the key to shoulder surfing and terminal logs.
**Learning:** In CLI applications, sensitive inputs must never echo. While the `pre_commit.py` script protected secrets from being hardcoded in commits, the interactive terminal prompt was still vulnerable.
**Prevention:** Always use the `getpass.getpass()` module for any sensitive user input prompts (API keys, tokens, passwords) instead of the standard `input()` function.
