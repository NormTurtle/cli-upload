## 2024-03-24 - API Key Input Masking
**Learning:** In CLI applications, user experience and security intersect when prompting for sensitive information like API keys. Using standard `input()` echoes the key to the terminal, which can be visible to shoulder surfers or captured in terminal history (if not careful), creating a poor and insecure experience.
**Action:** Always use Python's built-in `getpass.getpass()` instead of `input()` when prompting users for sensitive credentials (API keys, passwords, tokens) in CLI tools to mask the input and provide a standard, expected security UX.
