## 2024-05-24 - API Key Exposure in CLI Prompt
**Vulnerability:** The CLI tool `uc.py` requested sensitive API keys via standard `input()`.
**Learning:** Using `input()` for credentials exposes them on screen to shoulder surfers and saves them in some terminal histories or process logs.
**Prevention:** Always use the `getpass` module for sensitive CLI inputs to mask the keystrokes and prevent exposure.