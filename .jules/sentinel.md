## 2024-03-30 - Masking API Keys from Command Line Input
**Vulnerability:** The UC Files API key is prompted using Python's standard `input()` function, which exposes the 32-character hash API key in plain text on the terminal and potentially saves it to the terminal history.
**Learning:** We need to use `getpass.getpass()` instead of `input()` to securely prompt users for passwords, tokens, and API keys. This is a common pattern for CLI scripts interacting with sensitive credentials.
**Prevention:** Always default to `getpass` for prompting any credential or token in CLI scripts.
