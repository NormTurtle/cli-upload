
## 2024-05-15 - [File System Traversal Optimization]
**Learning:** In Python, `os.walk` yields a tuple `(root, dirs, files)` for each directory. When building a list of files to upload and determining their remote folder paths, any path resolution based on the `root` directory (like `os.path.relpath(root, folder_path)`) should be done *once per directory* rather than inside the inner loop over `files`. Doing it inside the inner loop recalculates identical strings for every single file in the directory, leading to an O(N) performance hit where N is the number of files, instead of O(D) where D is the number of directories.
**Action:** Always hoist invariant calculations (like directory-relative path strings) out of inner loops.
