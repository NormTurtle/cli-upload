## 2024-05-18 - Prevent getpass from Blocking Automated Tests
**Vulnerability:** Interactive prompts using `getpass` can cause automated integration tests (like `subprocess.run`) to hang and time out, potentially leading to CI failures or masking other issues if not handled properly.
**Learning:** When adding `getpass` to prompt for sensitive inputs to prevent shoulder surfing or history exposure, you must ensure integration tests are updated to supply input via `input='\n'` in `subprocess.run`.
**Prevention:** Always verify integration test suites after modifying CLI entry points to require interactive inputs, and use `input='\n'` in `subprocess.run` to bypass blocking behavior.
