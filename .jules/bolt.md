## 2024-04-06 - Stream Progress Updates Buffer
**Learning:** High frequency progress bar updates on small stream reads cause thread lock contention, hindering IO throughput.
**Action:** When tracking progress via streams (e.g., `_MultipartStream.read`), buffer progress updates and flush them at thresholds (like 512KB) instead of every single tiny chunk. Ensure cleanup handles un-flushed buffers correctly during `.close()` or `.seek()`.
