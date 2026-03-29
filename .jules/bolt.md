## 2024-03-29 - Reduce lock contention in stream reads

**Learning:** Threaded file uploads that use small read chunks can cause high lock contention when updating global progress variables on every read operation, leading to a performance bottleneck. This codebase uses a custom `_MultipartStream` that tracks actual network bytes sent.

**Action:** Implement a local progress buffer inside stream iterators like `_MultipartStream` to batch global `add_progress()` calls, thereby drastically reducing the frequency of acquiring the global lock. Make sure to accurately subtract un-flushed buffer bytes in `seek()` for correct retries without negative progress.
