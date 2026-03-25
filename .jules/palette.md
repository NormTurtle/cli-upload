## 2024-05-15 - Masking Sensitive CLI Inputs
**Learning:** In CLI applications, sensitive inputs like API keys and tokens should be masked (e.g., using Python's `getpass` module) to prevent shoulder surfing or exposing credentials in terminal history. This provides a more secure and reassuring user experience.
**Action:** Always use `getpass.getpass()` instead of `input()` when prompting users for sensitive information.
