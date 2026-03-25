## 2026-03-25 - Lock Contention in _MultipartStream
**Learning:** High lock contention during threaded file uploads (e.g., within _MultipartStream reading small chunks via requests) causes performance bottlenecks because global progress locks are hit on every tiny network read.
**Action:** Batch or buffer global progress updates locally and only flush them periodically rather than updating on every small read.
