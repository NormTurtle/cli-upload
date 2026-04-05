## 2025-04-05 - Folder Creation Lock Contention
**Learning:** During concurrent folder uploads, having worker threads dynamically create remote folders on-demand (`ensure_folder_exists`) leads to high lock contention and serializes the startup phase, defeating concurrency until folders are cached.
**Action:** Extract all unique remote folder paths prior to starting the thread pool and create them sequentially. This primes the cache so all concurrent workers hit the fast lock-free path.
