## 2024-05-20 - Masking Terminal Inputs
**Learning:** For command-line apps requesting sensitive tokens (like API keys), `input()` stores secrets in terminal logs and exposes them visually.
**Action:** Replace `input()` with `getpass.getpass()` for interactive CLI secrets, significantly enhancing UX and security simultaneously.
