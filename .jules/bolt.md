## 2024-05-15 - Batching global progress updates in threaded requests streams
**Learning:** `requests` reads streamed files in small 8KB chunks when passing `data=stream`. If each read updates a global progress bar protected by a lock, it causes severe lock contention under high concurrency, tanking performance.
**Action:** Always batch progress updates locally within the stream object (e.g. to 1MB) before acquiring the global lock to update the progress counter. Also ensure to flush the remainder on `close()` and correctly handle it in `seek()` rollbacks.
