
## 2024-05-24 - ThreadPoolExecutor Lock Contention During Concurrent Network Operations
**Learning:** Calling an API with a lock inside a worker function (e.g. `ensure_folder_exists` within `process_folder` using `folder_lock`) causes massive thread contention. Worker threads will wait sequentially for the lock, destroying the benefit of the `ThreadPoolExecutor` and delaying network requests for actual file uploads.
**Action:** Extract unique prerequisite data (e.g., remote folder paths) from the task list and resolve them sequentially *before* starting the `ThreadPoolExecutor`. This allows worker threads to hit the fast-path return (bypassing the lock) and execute their primary I/O bound tasks concurrently.
