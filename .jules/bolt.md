
## 2024-05-18 - Reduce Lock Contention with Buffered Progress Updates
**Learning:** In a multi-threaded file upload scenario, updating global progress (which requires locking and triggers UI redraws) on every small read chunk causes severe lock contention, degrading performance.
**Action:** Implement local buffer trackers inside stream reader classes. Accumulate bytes read locally and flush to the global progress tracker only after crossing a significant threshold (e.g., 512KB), significantly reducing the frequency of global lock acquisitions. Also ensure smooth rollback handles local vs global buffers when recovering from errors.
