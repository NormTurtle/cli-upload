## 2024-05-24 - Reduce Lock Contention in `_MultipartStream`
**Learning:** `requests` stream uploads read from custom file-like objects in small chunks (e.g., 8KB). If we update a global, lock-protected progress counter on every `read()` call, it creates massive lock contention across multiple upload threads.
**Action:** Buffer progress updates locally within the custom stream object (e.g., up to 512KB) before acquiring the global lock, and ensure rollback logic handles un-flushed buffers correctly.
