## 2024-03-22 - Hide API Key Input from Shoulder Surfing
**Vulnerability:** API key was prompted using `input()`, exposing it to shoulder surfing and potential terminal history logging.
**Learning:** Terminal inputs for sensitive credentials must always be masked.
**Prevention:** Always use `getpass.getpass()` for sensitive inputs instead of `input()`.
