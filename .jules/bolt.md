
## 2024-05-20 - Global Progress Lock Contention in Streaming Uploads
**Learning:** During concurrent chunked uploads (like `uc.py`'s 3 thread chunks), small synchronous stream reads (e.g. 128 KB blocks) were all synchronously writing progress updates. Every chunk invoked `add_progress()` triggering a thread-safe `with progress_lock:`, heavily throttling upload potential by hammering GIL with lock switching.
**Action:** When implementing buffered multipart streams or long running concurrent task progress tracking, implement class-scoped buffers (like `self._progress_buf`). Accumulate small progresses and only dump to the `global_lock` after satisfying an optimal threshold (e.g., `512 KB`) or upon stream reset/close.
