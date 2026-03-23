#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["requests"]
# ///

# --- CONFIG ---
API_KEY = "PASTE_API_KEY_HERE"  # ← paste your 32-char hash here or  ~/.uc_key
LOG_FILE = ".uc.log"  # default is .uc.log
KEY_FILE = "~/.uc_key"  # default is ~/.uc_key
RESUME_DIR = ""  # defaults to tempdir if empty
API_BASE = "https://files.union-crax.xyz"
VERSION = "0.1.1"  # current app version
FILE_THREADS = 5  # files uploaded in parallel (folder mode)
CHUNK_THREADS = 3  # chunk upload threads per large file (reduced to avoid 503 saturation)
DOWNLOAD_CONNS = 16  # parallel Range connections when downloading a URL
CHUNK_SIZE = 50 * 1024 * 1024  # optimistic default; auto-lowered if server rejects
MINI_CHUNK_SIZE = 128 * 1024  # 128 KB read buffer
MAX_SIMPLE_SIZE = 50 * 1024 * 1024  # files <= this go through simple upload
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds between retries

# --- IMPORTS ---
# ruff: noqa: E402
import argparse
import datetime
import hashlib
import io
import json
import math
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

# --- STATE ---
progress_lock = threading.Lock()
log_lock = threading.Lock()
active_uploads = {}
global_bytes_done = 0
is_folder_mode = False
progress_active = False

known_folders = set()  # folders we've already verified/created this run
_folders_fetched = False
_detected_chunk_limit_bytes = None
_detected_chunk_limit_lock = threading.Lock()

# --- SETUP (key loading, session creation) ---


def load_key(override_key=None):
    """Load the API key from --key flag, ~/.uc_key file, or prompt the user."""
    if override_key:
        return override_key.strip()
    key_path = Path(os.path.expanduser(KEY_FILE))
    if key_path.exists():
        key = key_path.read_text().strip()
        if key:
            return key
    return ""


def prompt_key():
    """Ask the user for their API key and validate it."""
    key = input("UC Files API Key (32-char hash): ").strip()
    if not key:
        print("No key provided. Exiting.")
        sys.exit(1)
    return key


def validate_key(session, key):
    """Check the key against GET /api/auth/me. Returns True if valid."""
    try:
        resp = session.get(
            f"{API_BASE}/api/auth/me",
            headers={"X-API-Key": key},
            timeout=15,
        )
        if resp.status_code == 200:
            return True
        if resp.status_code == 401:
            return False
        # treat other errors as validation failure
        log(f"Key validation returned status {resp.status_code}: {resp.text}")
        return False
    except requests.RequestException as exc:
        log(f"Key validation network error: {exc}")
        return False


def save_key(key):
    """Persist a validated key to ~/.uc_key with restrictive permissions."""
    key_path = Path(os.path.expanduser(KEY_FILE))
    key_path.write_text(key)
    try:
        key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600 — owner-only
    except OSError:
        pass  # Windows doesn't support Unix permissions


def make_session():
    """Build a requests.Session with large connection pools for threading."""
    session = requests.Session()
    # max_retries=0 here — api_request() handles retries to avoid double-retry (adapter × manual)
    adapter = HTTPAdapter(pool_connections=32, pool_maxsize=64, max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers["User-Agent"] = "uc-cli/1.0"
    return session


def auth_headers():
    """Return the auth header dict. Every request needs this."""
    return {"X-API-Key": API_KEY}


# --- UTILITIES (log, human_size, human_time, etc.) ---


def log(msg):
    """Append a timestamped message to .uc.log cleanly across threads."""
    try:
        with log_lock, open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass  # never crash because of logging


def human_size(n):
    """Convert byte count to a readable string like '4.56 GB'."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(n) < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def human_time(seconds):
    """Convert seconds to MM:SS or HH:MM:SS if >= 1 hour."""
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _resume_state_path(folder_path, target_folder_name):
    global RESUME_DIR
    raw = f"{os.path.abspath(folder_path)}|{target_folder_name}"
    key = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    base_dir = RESUME_DIR if RESUME_DIR else tempfile.gettempdir()
    return Path(base_dir) / f".uc_resume_{key}.json"


def _load_resume_state(state_path):
    if not state_path.exists():
        return {"version": 1, "done": {}}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"version": 1, "done": {}}
        done = data.get("done", {})
        if not isinstance(done, dict):
            done = {}
        return {"version": 1, "done": done}
    except Exception as exc:
        log(f"Resume state read failed ({state_path}): {exc}")
        return {"version": 1, "done": {}}


def _save_resume_state(state_path, state):
    try:
        tmp = state_path.with_suffix(state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(state_path)
    except Exception as exc:
        log(f"Resume state write failed ({state_path}): {exc}")


def ok_symbol():
    """Return a check mark or [OK] depending on terminal encoding support."""
    try:
        if sys.stdout.encoding and "utf" in sys.stdout.encoding.lower():
            return "✓"
    except Exception:
        pass
    return "[OK]"


def api_request(session, method, path, retries=MAX_RETRIES, **kwargs):
    """
    Make an API request with automatic retries on transient errors.
    Handles 401 (bad key) and 413 (chunk too large) specially.
    """
    url = f"{API_BASE}{path}"
    kwargs.setdefault("headers", {})
    kwargs["headers"].update(auth_headers())
    kwargs.setdefault("timeout", 60)
    log(f">> {method} {path}")

    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.request(method, url, **kwargs)
            resp_len = resp.headers.get("Content-Length", "?")
            log(f"<< {method} {path} -> {resp.status_code} ({resp_len} bytes)")

            if resp.status_code == 401:
                print("\nInvalid API key — delete ~/.uc_key and re-run.")
                log("FATAL: 401 Unauthorized — API key rejected")
                os._exit(1)

            if resp.status_code == 413:
                # Don't hard-exit the entire run; let caller fail this item and continue.
                detail = resp.text.strip()[:500]
                log(f"HTTP 413 on {method} {path}: {detail}")
                raise requests.HTTPError(
                    f"413 Payload Too Large on {method} {path}: {detail}",
                    response=resp,
                )

            if resp.status_code >= 500:
                log(
                    f"RETRY: server error {resp.status_code} on {method} {path} (attempt {attempt}), body: {resp.text[:500]}"
                )
                last_exc = Exception(f"HTTP {resp.status_code}")
                if attempt < retries:
                    # The backend explicitly reports worker saturation on 503.
                    # Back off progressively so we recover instead of failing a whole file.
                    delay = RETRY_DELAY
                    if resp.status_code == 503:
                        delay = min(30, RETRY_DELAY * attempt)
                    time.sleep(delay)
                continue

            if 400 <= resp.status_code < 500:
                log(f"CLIENT ERROR body on {method} {path}: {resp.text[:1000]}")

            # log response body for API calls that return JSON (useful for debugging field names)
            if "/api/" in path and resp.headers.get("content-type", "").startswith(
                "application/json"
            ):
                log(f"   response json: {resp.text[:1000]}")

            return resp

        except requests.RequestException as exc:
            log(f"Request error on {method} {path} (attempt {attempt}): {exc}")
            last_exc = exc
            if attempt < retries:
                time.sleep(RETRY_DELAY)
                if "data" in kwargs and hasattr(kwargs["data"], "seek"):
                    try:
                        kwargs["data"].seek(0)
                    except Exception as e:
                        log(f"Stream seek failed: {e}")

                # If the connection pool was poisoned by a network drop,
                # forcefully bypass the pool completely for this retry.
                if isinstance(
                    exc,
                    (
                        requests.exceptions.ConnectionError,
                        requests.exceptions.ChunkedEncodingError,
                    ),
                ):
                    log(f"Bypassing session pool for retry due to connection error: {exc}")
                    session = requests

    raise last_exc


# --- PROGRESS BAR ---


def _term_width():
    """Get terminal width, fallback to 80 for dumb terminals."""
    try:
        return shutil.get_terminal_size((80, 24)).columns
    except Exception:
        return 80


def _clear_drawn_lines():
    width = _term_width()
    sys.stdout.write("\r" + " " * (width - 1) + "\r")
    sys.stdout.flush()


def start_progress(filename, total_size, folder_mode=False):
    global progress_active, is_folder_mode
    with progress_lock:
        if progress_active:
            _clear_drawn_lines()

        if not progress_active:
            is_folder_mode = folder_mode
            progress_active = True
            t = threading.Thread(target=_draw_loop, daemon=True)
            t.start()

        active_uploads[filename] = {
            "done": 0,
            "total": total_size,
            "start_time": time.time(),
        }


def finish_progress(filename, size, url, elapsed=None, speed=None):
    global progress_active
    with progress_lock:
        _clear_drawn_lines()
        if filename in active_uploads:
            del active_uploads[filename]

        if url and not url.startswith("http"):
            url = f"{API_BASE}{url}"

        if is_folder_mode:
            print(f" {ok_symbol()} {filename} [{human_size(size)}]")
        else:
            if elapsed is not None and speed is not None and speed > 0:
                print(f"{human_time(elapsed)} - {human_size(speed)}/s")
            print(f" {ok_symbol()} {filename} [{human_size(size)}]")
            if url:
                print(url)

        if not active_uploads:
            progress_active = False


def fail_progress(filename, exc):
    global progress_active
    with progress_lock:
        _clear_drawn_lines()
        if filename in active_uploads:
            del active_uploads[filename]
        print(f" [X] FAIL: {filename} -> {exc}")
        if not active_uploads:
            progress_active = False


def add_progress(filename, n):
    global global_bytes_done
    with progress_lock:
        if filename in active_uploads:
            active_uploads[filename]["done"] += n
        global_bytes_done += n


def _draw_loop():
    global global_bytes_done, progress_active
    last_global_done = global_bytes_done
    last_time = time.time()
    speed_window = []

    while True:
        with progress_lock:
            if not progress_active and not active_uploads:
                return

            if active_uploads:
                _clear_drawn_lines()

                total_bytes = sum(s["total"] for s in active_uploads.values())
                done_bytes = sum(s["done"] for s in active_uploads.values())

                now = time.time()
                dt = now - last_time
                if dt > 0:
                    inst_speed = max(0, global_bytes_done - last_global_done) / dt
                    speed_window.append(inst_speed)
                    speed_window = speed_window[-5:]

                last_time = now
                last_global_done = global_bytes_done

                avg_speed = sum(speed_window) / len(speed_window) if speed_window else 0

                if total_bytes > 0:
                    pct = int(done_bytes / total_bytes * 100)
                else:
                    pct = 0

                if avg_speed > 0 and total_bytes > 0:
                    eta_secs = max(0, total_bytes - done_bytes) / avg_speed
                    time_str = str(datetime.timedelta(seconds=int(eta_secs)))
                else:
                    time_str = "--:--"

                # compute best file
                best_file = ""
                best_pct = -1
                for fname, stats in active_uploads.items():
                    if stats["total"] > 0:
                        p = stats["done"] / stats["total"]
                        if p > best_pct:
                            best_pct = p
                            best_file = fname

                width = _term_width()

                header_line = f"Uploading: {best_file}"
                if len(active_uploads) > 1:
                    header_line += f" (+{len(active_uploads) - 1} others)"
                if len(header_line) > width - 1:
                    header_line = header_line[: width - 4] + "..."
                header_line = header_line.strip()

                speed_str = f"{human_size(avg_speed)}/s"
                done_str = human_size(done_bytes)
                total_str = human_size(total_bytes)

                if done_bytes >= total_bytes and total_bytes > 0:
                    time_str = "Finalizing"
                    speed_str = "---"

                base_str = (
                    f"[{time_str}] [{speed_str}] [ {pct:>3}%] {header_line} {done_str}/{total_str}"
                )

                bar_len = max(5, (width - 1) - len(base_str))
                filled = int(bar_len * done_bytes / total_bytes) if total_bytes > 0 else 0
                bar = "#" * filled + "-" * (bar_len - filled)

                line = f"[{time_str}] [{speed_str}] [{bar} {pct:>3}%] {header_line} {done_str}/{total_str}"

                if len(line) > width - 1:
                    line = line[: width - 1]

                sys.stdout.write(f"\r{line}")
                sys.stdout.flush()

        time.sleep(0.2)


# --- FOLDER MANAGEMENT ---

folder_lock = threading.Lock()

# maps folder name -> folder url/id (populated by list and create calls)
folder_urls = {}


def ensure_folders_cached(session):
    """Fetch the folder list once and populate known_folders set."""
    global _folders_fetched
    with folder_lock:
        if _folders_fetched:
            return
        log("Fetching folder list from API")
        try:
            resp = api_request(session, "GET", "/api/folders")
            data = resp.json()
            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("folders", data.get("data", []))
            log(f"Found {len(items)} folders on server")
            for folder in items:
                if isinstance(folder, dict):
                    name = folder.get("name", "")
                    if name:
                        known_folders.add(name)
                        # capture any url/id the API gives us
                        furl = folder.get("url", folder.get("link", ""))
                        fid = folder.get("id", folder.get("folder_id", ""))
                        if furl:
                            folder_urls[name] = furl
                        elif fid:
                            folder_urls[name] = f"{API_BASE}/folder/{fid}"
            _folders_fetched = True
        except Exception as exc:
            log(f"Failed to list folders: {exc}")


def ensure_folder_exists(session, folder_name):
    """Create a remote folder if it doesn't already exist. Caches results."""
    if not folder_name:
        return
    # fast path outside lock
    if folder_name in known_folders:
        return

    ensure_folders_cached(session)

    with folder_lock:
        if folder_name in known_folders:
            return
        log(f"Creating folder: {folder_name}")
        try:
            resp = api_request(
                session,
                "POST",
                "/api/folders/create",
                json={"name": folder_name},
            )
            known_folders.add(folder_name)
            # capture folder url from creation response
            rdata = resp.json()
            log(f"Folder create response: {rdata}")
            furl = rdata.get("url", rdata.get("link", ""))
            fid = rdata.get("id", rdata.get("folder_id", ""))
            if furl:
                folder_urls[folder_name] = furl
            elif fid:
                folder_urls[folder_name] = f"{API_BASE}/folder/{fid}"
        except Exception as exc:
            log(f"FAIL: create folder '{folder_name}': {exc}")


# --- UPLOAD: SMALL FILE ---


def _build_multipart(filename, filepath, file_size, folder=""):
    """
    Build a multipart/form-data body manually so we can stream it via data=
    and track actual network bytes sent (not just file reads buffered in memory).
    Returns (boundary, body_stream) where body_stream is a _MultipartStream.
    """
    safe_filename = filename.replace('"', "_")
    boundary = f"----UCUpload{secrets.token_hex(16)}"
    parts_header = b""
    # optional folder field
    if folder:
        parts_header += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="folder"\r\n\r\n{folder}\r\n'
        ).encode()
    # file field header
    parts_header += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe_filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    parts_footer = f"\r\n--{boundary}--\r\n".encode()
    total_body = len(parts_header) + file_size + len(parts_footer)
    stream = _MultipartStream(
        parts_header,
        open(filepath, "rb"),
        parts_footer,
        total_body,
        file_size,
        filename,
    )
    return boundary, stream, total_body


class _BoundedFile:
    def __init__(self, filepath, offset, length):
        self.f = open(filepath, "rb")
        self.original_offset = offset
        self.original_length = length
        self.f.seek(offset)
        self.remaining = length

    def read(self, size=-1):
        if self.remaining <= 0:
            return b""
        if size < 0 or size > self.remaining:
            size = self.remaining
        chunk = self.f.read(size)
        if not chunk:
            return b""
        self.remaining -= len(chunk)
        return chunk

    def seek(self, offset):
        if offset != 0:
            raise ValueError("Only seek(0) is supported for bounded retry")
        self.f.seek(self.original_offset)
        self.remaining = self.original_length

    def close(self):
        self.f.close()


def _build_multipart_chunk(filename, filepath, upload_id, index, offset, length):
    boundary = f"----UCChunk{secrets.token_hex(16)}{index}"
    parts_header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="upload_id"\r\n\r\n'
        f"{upload_id}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="index"\r\n\r\n'
        f"{index}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chunk"; filename="chunk_{index}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    parts_footer = f"\r\n--{boundary}--\r\n".encode()

    total_body = len(parts_header) + length + len(parts_footer)
    file_obj = _BoundedFile(filepath, offset, length)
    stream = _MultipartStream(parts_header, file_obj, parts_footer, total_body, length, filename)
    return boundary, stream, total_body


class _MultipartStream:
    """
    A file-like object that concatenates: header bytes + file on disk + footer bytes.
    Tracks upload progress via add_progress() on every read() call so progress
    reflects actual bytes sent over the network, not file buffering.
    """

    def __init__(self, header, file_obj, footer, total, file_size, filename):
        self._parts = [
            io.BytesIO(header),
            file_obj,
            io.BytesIO(footer),
        ]
        self._idx = 0
        self._total = total
        self._header_len = len(header)
        self._footer_len = len(footer)
        self._file_size = file_size
        self._filename = filename
        self._bytes_read = 0

    def read(self, n=-1):
        """Read n bytes across parts, updating progress for file content bytes."""
        if n is None or n < 0:
            result = b"".join(p.read() for p in self._parts[self._idx :])
            self._idx = len(self._parts)
            file_bytes = max(0, len(result) - max(0, self._header_len - self._bytes_read))
            if file_bytes > 0:
                add_progress(self._filename, min(file_bytes, self._file_size))
            self._bytes_read += len(result)
            return result

        result = b""
        remaining = n
        while remaining > 0 and self._idx < len(self._parts):
            chunk = self._parts[self._idx].read(remaining)
            if chunk:
                before = self._bytes_read
                self._bytes_read += len(chunk)
                result += chunk
                remaining -= len(chunk)
                # count only bytes that fall inside the file region
                file_start = self._header_len
                file_end = self._header_len + self._file_size
                prog = max(0, min(self._bytes_read, file_end) - max(before, file_start))
                if prog > 0:
                    add_progress(self._filename, prog)
            else:
                self._idx += 1
        return result

    def __len__(self):
        return self._total

    def seek(self, offset):
        if offset != 0:
            raise ValueError("Only seek(0) is supported for stream retry")
        for p in self._parts:
            p.seek(0)
        self._idx = 0
        # undo previously reported progress so retries don't double-count
        file_progress_reported = max(
            0,
            min(self._bytes_read, self._header_len + self._file_size) - self._header_len,
        )
        if file_progress_reported > 0:
            add_progress(self._filename, -file_progress_reported)
        self._bytes_read = 0

    def close(self):
        for p in self._parts:
            try:
                p.close()
            except Exception:
                pass


def upload_small(session, filepath, folder="", folder_mode=False):
    """Upload a file <= 50 MB using the simple POST /api/upload endpoint."""
    filename = os.path.basename(filepath)
    file_size = os.path.getsize(filepath)

    log(f"UPLOAD SMALL: {filename} ({human_size(file_size)}) -> folder={folder!r}")
    start_progress(filename, file_size, folder_mode)
    t0 = time.time()
    try:
        # build multipart body manually so we stream it and track real upload progress
        boundary, stream, body_len = _build_multipart(filename, filepath, file_size, folder)
        try:
            resp = api_request(
                session,
                "POST",
                "/api/upload",
                data=stream,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(body_len),
                },
                timeout=300,
            )
        finally:
            stream.close()
        elapsed = time.time() - t0
        avg_speed = file_size / elapsed if elapsed > 0 else 0
        result = resp.json()
        log(f"Upload response: {result}")
        url = result.get("url", result.get("link", ""))
        file_id = result.get("id", result.get("file_id", ""))
        if not url and file_id:
            url = f"{API_BASE}/f/{file_id}"
        log(f"DONE: {filename} -> {url} in {human_time(elapsed)} at {human_size(avg_speed)}/s")
        finish_progress(filename, file_size, url, elapsed, avg_speed)
        return url
    except Exception as exc:
        log(f"FAIL: small upload {filename}: {exc}")
        fail_progress(filename, exc)
        return None


# --- UPLOAD: LARGE FILE (CHUNKED) ---


def _extract_chunk_limit_bytes(text):
    """Parse server-provided chunk size limit from plain-text error responses."""
    m = re.search(r"server limit of\s+(\d+)\s+bytes", text or "", flags=re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _get_effective_chunk_size():
    with _detected_chunk_limit_lock:
        return _detected_chunk_limit_bytes or CHUNK_SIZE


def _set_detected_chunk_limit(limit_bytes):
    global _detected_chunk_limit_bytes
    if not limit_bytes or limit_bytes <= 0:
        return
    with _detected_chunk_limit_lock:
        if _detected_chunk_limit_bytes is None or limit_bytes < _detected_chunk_limit_bytes:
            _detected_chunk_limit_bytes = limit_bytes
            log(f"Detected server chunk cap: {limit_bytes} bytes ({human_size(limit_bytes)})")


def _init_chunked_upload(session, filename, file_size, folder):
    """Initialize chunked upload and adapt to server-side chunk cap when necessary."""
    chunk_size = _get_effective_chunk_size()

    for _attempt in range(2):
        chunk_count = math.ceil(file_size / chunk_size)
        resp = api_request(
            session,
            "POST",
            "/api/upload/init",
            json={
                "filename": filename,
                "size": file_size,
                "chunk_count": chunk_count,
                "folder": folder,
            },
        )

        if resp.status_code == 200:
            data = resp.json()
            upload_id = data.get("upload_id", data.get("uploadId", ""))
            if upload_id:
                return upload_id, chunk_size, chunk_count, data
            raise RuntimeError(f"Chunk init missing upload_id: {data}")

        body = (resp.text or "").strip()
        limit_bytes = _extract_chunk_limit_bytes(body)
        if resp.status_code == 400 and limit_bytes:
            _set_detected_chunk_limit(limit_bytes)
            if limit_bytes != chunk_size:
                chunk_size = limit_bytes
                continue

        raise RuntimeError(f"Chunk init failed ({resp.status_code}): {body[:300]}")

    raise RuntimeError("Chunk init failed after adaptive retry")


def upload_large(session, filepath, folder="", folder_mode=False):
    """Upload a file > 50 MB using chunked init → chunk uploads → finish → poll."""
    filename = os.path.basename(filepath)
    file_size = os.path.getsize(filepath)

    # step 1: init the chunked upload
    try:
        upload_id, chunk_size, chunk_count, _init_data = _init_chunked_upload(
            session, filename, file_size, folder
        )
    except Exception as exc:
        log(f"Chunked init exception for {filename}: {exc}")
        print(f"[X] FAIL: {filename} -> chunked init failed: {exc}")
        return None

    log(
        f"UPLOAD LARGE: {filename} ({human_size(file_size)}) -> {chunk_count} chunks @ {human_size(chunk_size)}, folder={folder!r}"
    )

    t0 = time.time()
    start_progress(filename, file_size, folder_mode)

    # step 2: upload chunks in parallel
    failed = False
    with ThreadPoolExecutor(max_workers=CHUNK_THREADS) as pool:
        futures = []
        for i in range(chunk_count):
            offset = i * chunk_size
            length = min(chunk_size, file_size - offset)
            fut = pool.submit(
                _upload_one_chunk,
                session,
                filepath,
                filename,
                upload_id,
                i,
                offset,
                length,
            )
            futures.append(fut)

        for fut in as_completed(futures):
            if fut.exception():
                log(f"Chunk upload exception: {fut.exception()}")
                failed = True

    if failed:
        fail_progress(filename, "Some chunks failed. Check log.")
        return None

    # step 3: finish
    finish_resp = api_request(
        session,
        "POST",
        "/api/upload/finish",
        json={"upload_id": upload_id},
    )
    finish_data = finish_resp.json()

    # step 4: use finish URL directly (best throughput), poll only if URL is missing
    file_id = finish_data.get("id", finish_data.get("file_id", ""))
    fallback_url = finish_data.get("url", finish_data.get("link", ""))
    url = fallback_url or (f"{API_BASE}/f/{file_id}" if file_id else "")
    if not url:
        url = _poll_until_ready(session, upload_id, file_id=file_id, max_wait=120) or ""

    elapsed = time.time() - t0
    avg_speed = file_size / elapsed if elapsed > 0 else 0
    finish_progress(filename, file_size, url, elapsed, avg_speed)
    return url


def _upload_one_chunk(session, filepath, filename, upload_id, index, offset, length):
    """Read one chunk from disk and POST it to /api/upload/chunk smoothly."""
    boundary, stream, body_len = _build_multipart_chunk(
        filename, filepath, upload_id, index, offset, length
    )

    # retry wrapper is inside api_request already
    try:
        api_request(
            session,
            "POST",
            "/api/upload/chunk",
            data=stream,
            retries=8,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(body_len),
            },
            timeout=120,
        )
    finally:
        stream.close()


def _poll_until_ready(session, upload_id, file_id="", max_wait=300):
    """Poll GET /api/upload/status until the file is ready or we time out."""
    deadline = time.time() + max_wait
    ids_to_try = [upload_id]
    if file_id and file_id not in ids_to_try:
        ids_to_try.append(file_id)

    while time.time() < deadline:
        for status_id in ids_to_try:
            try:
                resp = api_request(session, "GET", f"/api/upload/status?id={status_id}", retries=1)
                if resp.status_code == 404:
                    # Some uploads are visible under upload_id before file_id (or vice versa).
                    continue

                data = resp.json()
                status = data.get("status", "")
                if status == "ready":
                    return data.get("url", data.get("link", ""))
                if status in ("error", "failed"):
                    log(f"Upload processing failed for {status_id}: {data}")
                    return None
            except Exception as exc:
                log(f"Poll error for {status_id}: {exc}")
        time.sleep(2)

    log(f"Poll timed out after {max_wait}s for ids={ids_to_try}")
    return None


# --- DOWNLOAD + UPLOAD: URL MODE ---


def process_url(session, url, folder="", folder_mode=False):
    """Download a remote file and upload it to UC Files simultaneously."""
    # step 1: HEAD to get size and filename
    filename, file_size, supports_range = _probe_url(session, url)

    if file_size and file_size > MAX_SIMPLE_SIZE and supports_range:
        # pipe mode: download chunks via Range requests and upload them in a pipeline
        _pipe_upload(session, url, filename, file_size, folder, folder_mode)
    else:
        # fallback: download the whole file first, then upload normally
        _fallback_download_and_upload(session, url, filename, file_size, folder, folder_mode)


def _ext_from_content_type(content_type):
    """Return a file extension for a MIME type, or empty string if unknown."""
    mapping = {
        "video/mp4": ".mp4",
        "video/x-matroska": ".mkv",
        "application/zip": ".zip",
        "application/x-rar-compressed": ".rar",
        "application/octet-stream": ".bin",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "text/plain": ".txt",
    }
    base = content_type.split(";")[0].strip().lower()
    return mapping.get(base, "")


def _probe_url(session, url):
    """HEAD the URL to discover filename, size, and Range support."""
    filename = _filename_from_url(url)
    file_size = None
    supports_range = False

    try:
        resp = session.head(url, timeout=15, allow_redirects=True)
        resp.raise_for_status()

        # content length
        cl = resp.headers.get("Content-Length")
        if cl and cl.isdigit():
            file_size = int(cl)

        content_type = resp.headers.get("Content-Type", "")

        # accept ranges — only trust explicit "bytes" (absent header ≠ support)
        if resp.headers.get("Accept-Ranges", "").lower() == "bytes":
            supports_range = True

        # filename from Content-Disposition
        cd = resp.headers.get("Content-Disposition", "")
        if "filename=" in cd:
            parts = cd.split("filename=")[-1].strip().strip('"').strip("'")
            if parts:
                filename = os.path.basename(parts)  # sanitize: strip path traversal

        if "." not in filename:
            ext = _ext_from_content_type(content_type)
            if ext:
                filename = filename + ext

    except Exception as exc:
        log(f"HEAD failed for {url}: {exc}")

    return filename, file_size, supports_range


def _filename_from_url(url):
    """Extract a filename from a URL path."""
    from urllib.parse import unquote, urlparse

    path = urlparse(url).path
    name = unquote(os.path.basename(path))
    return name if name else "download"


def _pipe_upload(session, url, filename, file_size, folder, folder_mode=False):
    """
    Multi-segment pipe: for each chunk, download its byte range using DOWNLOAD_CONNS
    parallel mini-segments, then upload the chunk immediately. Pipeline chunk i+1
    download while chunk i uploads.
    """

    # init chunked upload (adaptive chunk sizing)
    try:
        upload_id, chunk_size, chunk_count, _init_data = _init_chunked_upload(
            session, filename, file_size, folder
        )
    except Exception as exc:
        log(f"Pipe init failed for {filename}: {exc}")
        print(f"\nChunked upload init failed for {filename}: {exc}")
        return

    t0 = time.time()
    start_progress(filename, file_size, folder_mode)

    # semaphore limits memory: at most 2 chunks buffered at once
    sem = threading.Semaphore(2)
    upload_pool = ThreadPoolExecutor(max_workers=2)
    upload_futures = []

    for i in range(chunk_count):
        byte_start = i * chunk_size
        byte_end = min((i + 1) * chunk_size, file_size) - 1
        chunk_len = byte_end - byte_start + 1

        sem.acquire()

        # download chunk i using parallel mini-segments
        chunk_data = _download_chunk_parallel(
            session, url, filename, byte_start, byte_end, chunk_len
        )
        if chunk_data is None:
            fail_progress(filename, f"Failed to download chunk {i}")
            return

        # upload chunk i in background while we start downloading chunk i+1
        fut = upload_pool.submit(_upload_pipe_chunk, session, upload_id, i, chunk_data, sem)
        upload_futures.append(fut)

    # wait for all uploads to finish and check for failures
    pipe_failed = False
    for fut in upload_futures:
        try:
            fut.result()
        except Exception as exc:
            log(f"Pipe upload future error: {exc}")
            pipe_failed = True

    upload_pool.shutdown(wait=True)

    if pipe_failed:
        fail_progress(filename, "Some pipe chunks failed. Check log.")
        return

    # finish
    finish_resp = api_request(
        session,
        "POST",
        "/api/upload/finish",
        json={"upload_id": upload_id},
    )
    finish_data = finish_resp.json()

    file_id = finish_data.get("id", finish_data.get("file_id", ""))
    fallback_url = finish_data.get("url", finish_data.get("link", ""))
    result_url = fallback_url or (f"{API_BASE}/f/{file_id}" if file_id else "")
    if not result_url:
        result_url = _poll_until_ready(session, upload_id, file_id=file_id, max_wait=120) or ""

    elapsed = time.time() - t0
    avg_speed = file_size / elapsed if elapsed > 0 else 0
    finish_progress(filename, file_size, result_url, elapsed, avg_speed)


def _download_chunk_parallel(session, url, filename, byte_start, byte_end, chunk_len):
    """
    Download a byte range [byte_start, byte_end] using DOWNLOAD_CONNS parallel
    mini-range GET requests. Each mini-segment writes into a pre-allocated bytearray
    at the correct offset.
    """
    chunk_data = bytearray(chunk_len)
    seg_size = math.ceil(chunk_len / DOWNLOAD_CONNS)

    def fetch_segment(seg_index):
        s_start = byte_start + seg_index * seg_size
        s_end = min(byte_start + (seg_index + 1) * seg_size - 1, byte_end)
        local_offset = seg_index * seg_size

        for attempt in range(MAX_RETRIES):
            try:
                resp = session.get(
                    url,
                    headers={"Range": f"bytes={s_start}-{s_end}"},
                    timeout=60,
                    stream=True,
                )
                received = 0
                for piece in resp.iter_content(chunk_size=MINI_CHUNK_SIZE):
                    n = len(piece)
                    chunk_data[local_offset + received : local_offset + received + n] = piece
                    received += n
                    add_progress(filename, n)
                return True
            except Exception as exc:
                log(f"Segment download error (seg {seg_index}, attempt {attempt + 1}): {exc}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
        return False

    with ThreadPoolExecutor(max_workers=DOWNLOAD_CONNS) as pool:
        seg_count = math.ceil(chunk_len / seg_size) if seg_size > 0 else 1
        futures = [pool.submit(fetch_segment, i) for i in range(seg_count)]
        for fut in as_completed(futures):
            if not fut.result():
                return None

    return bytes(chunk_data)


def _upload_pipe_chunk(session, upload_id, index, chunk_data, sem):
    """Upload a single chunk and release the semaphore when done."""
    try:
        api_request(
            session,
            "POST",
            "/api/upload/chunk",
            files={"chunk": (f"chunk_{index}", chunk_data, "application/octet-stream")},
            data={"upload_id": upload_id, "index": str(index)},
            retries=8,
            timeout=120,
        )
    except Exception as exc:
        log(f"Pipe chunk {index} upload failed: {exc}")
        raise  # propagate so the caller can detect failure
    finally:
        sem.release()


def _fallback_download_and_upload(session, url, filename, file_size, folder, folder_mode=False):
    """Download the entire file to a temp location, then upload using normal Mode A logic."""
    import hashlib

    file_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    safe_name = f"{file_hash}_{filename}"

    # pick temp dir: cross-platform temp location
    tmp_dir = os.path.join(tempfile.gettempdir(), "uc_downloads")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, safe_name)

    # support resuming a partial download
    existing_size = 0
    if os.path.exists(tmp_path):
        existing_size = os.path.getsize(tmp_path)

    dl_headers = {}
    if existing_size > 0:
        dl_headers["Range"] = f"bytes={existing_size}-"

    dl_name = f"(downloading) {filename}"
    display_total = file_size if file_size else 0
    start_progress(dl_name, display_total if display_total else 1)

    downloaded_bytes = 0
    try:
        resp = session.get(url, headers=dl_headers, stream=True, timeout=60)
        resp.raise_for_status()

        mode = "ab" if existing_size > 0 else "wb"
        with open(tmp_path, mode) as f:
            for piece in resp.iter_content(chunk_size=MINI_CHUNK_SIZE):
                f.write(piece)
                downloaded_bytes += len(piece)
                add_progress(dl_name, len(piece))
    except Exception as exc:
        log(f"Fallback download failed for {url}: {exc}")
        fail_progress(dl_name, exc)
        return

    finish_progress(dl_name, existing_size + downloaded_bytes, "")

    # now upload normally
    process_file(session, tmp_path, folder, folder_mode)

    # clean up temp file
    try:
        os.remove(tmp_path)
    except OSError:
        pass


# --- ORCHESTRATOR (process_file, process_url, process_folder) ---


def process_file(session, filepath, folder="", folder_mode=False):
    """Decide whether to use small or chunked upload for a local file."""
    file_size = os.path.getsize(filepath)
    if folder:
        ensure_folder_exists(session, folder)
    if file_size <= MAX_SIMPLE_SIZE:
        return upload_small(session, filepath, folder, folder_mode)
    else:
        return upload_large(session, filepath, folder, folder_mode)


def process_folder(session, folder_path, dest_folder="", resume=True):
    """Walk a local folder and upload every file one at a time, preserving subfolder structure."""
    folder_path = os.path.normpath(folder_path)
    base_name = os.path.basename(folder_path)
    target_folder = dest_folder if dest_folder else base_name

    # persistent checkpoint so retries continue where they left off
    state_path = _resume_state_path(folder_path, target_folder)
    if resume:
        resume_state = _load_resume_state(state_path)
    else:
        resume_state = {"version": 1, "done": {}}

    # build list of (local_path, remote_folder) tuples
    to_upload = []
    skipped_count = 0
    skipped_bytes = 0
    for root, _dirs, files in os.walk(folder_path):
        for fname in files:
            if fname == LOG_FILE:
                continue
            local_path = os.path.join(root, fname)
            rel_file = os.path.relpath(local_path, folder_path).replace("\\", "/")
            st = os.stat(local_path)
            size = st.st_size
            mtime_ns = st.st_mtime_ns
            rel_dir = os.path.relpath(root, folder_path)

            if dest_folder:
                if rel_dir == ".":
                    remote_folder = dest_folder
                else:
                    remote_folder = f"{dest_folder}/{rel_dir}".replace("\\", "/")
            else:
                if rel_dir == ".":
                    remote_folder = base_name
                else:
                    remote_folder = f"{base_name}/{rel_dir}".replace("\\", "/")

            done_entry = resume_state["done"].get(rel_file)
            if (
                isinstance(done_entry, dict)
                and done_entry.get("size") == size
                and done_entry.get("mtime_ns") == mtime_ns
            ):
                skipped_count += 1
                skipped_bytes += size
                continue

            to_upload.append((local_path, remote_folder, rel_file, size, mtime_ns))

    if not to_upload:
        print("No pending files found in the folder (resume checkpoint says all done).")
        return

    # ensure all needed remote folders exist (deduplicated)
    ensure_folders_cached(session)
    remote_dirs = set(item[1] for item in to_upload if item[1])
    for rd in remote_dirs:
        ensure_folder_exists(session, rd)

    total_size = sum(f[3] for f in to_upload)
    log(
        f"FOLDER: pending={len(to_upload)} files ({human_size(total_size)}), "
        f"skipped={skipped_count} files ({human_size(skipped_bytes)}), dest={dest_folder!r}"
    )
    print(
        f"Uploading pending {len(to_upload)} file(s) ({human_size(total_size)}), "
        f"skipping {skipped_count} already-completed file(s) ({human_size(skipped_bytes)})\n"
    )

    t0_folder = time.time()
    success_count = 0
    fail_count = 0

    with ThreadPoolExecutor(max_workers=FILE_THREADS) as pool:
        futures = {}
        for path, remote_dir, rel_file, size, mtime_ns in to_upload:
            fut = pool.submit(process_file, session, path, remote_dir, True)
            futures[fut] = (rel_file, size, mtime_ns)
        for fut in as_completed(futures):
            rel_file, size, mtime_ns = futures[fut]
            try:
                result = fut.result()
                if result:
                    success_count += 1
                    resume_state["done"][rel_file] = {
                        "size": size,
                        "mtime_ns": mtime_ns,
                        "url": result,
                        "uploaded_at": int(time.time()),
                    }
                    _save_resume_state(state_path, resume_state)
                else:
                    fail_count += 1
            except Exception as exc:
                log(f"Folder upload error: {exc}")
                fail_count += 1

    # folder summary at the end
    elapsed = time.time() - t0_folder
    avg_speed = total_size / elapsed if elapsed > 0 else 0
    folder_link = folder_urls.get(target_folder, "")
    if folder_link and not folder_link.startswith("http"):
        folder_link = f"{API_BASE}{folder_link}"

    log(
        f"FOLDER DONE: {target_folder} pending=[{human_size(total_size)}] in {human_time(elapsed)} at {human_size(avg_speed)}/s, "
        f"success={success_count}, failed={fail_count}, skipped={skipped_count}, checkpoint={str(state_path)!r}, link={folder_link!r}"
    )
    print(
        f"{target_folder} [{human_size(total_size)} pending] - {human_time(elapsed)} "
        f"(success={success_count}, failed={fail_count}, skipped={skipped_count})"
    )
    if folder_link:
        print(folder_link)

    if fail_count > 0:
        raise RuntimeError(
            f"Folder upload incomplete: {success_count} succeeded, {fail_count} failed"
        )


# --- MAIN (argparse, entrypoint) ---


def main():
    global API_KEY, LOG_FILE, KEY_FILE, RESUME_DIR

    prog_name = os.path.basename(sys.argv[0])
    if prog_name == "uc.py":
        prog_name = "python uc.py"
    elif prog_name == "__main__.py":
        prog_name = "python -m uc"
    elif "uv" in sys.argv[0] and "cache" in sys.argv[0]:
        prog_name = "uvx ucf"
    else:
        prog_name = "ucf"

    parser = argparse.ArgumentParser(
        prog=prog_name,
        description="uc.py — blazing-fast CLI uploader for UC Files (files.union-crax.xyz)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python uc.py video.mp4\n"
            "  uvx ucf /home/user/movies -d Films\n"
            "  uvx ucf https://example.com/archive.zip -d Downloads\n"
            "  uv run uc.py big_folder/ -d Backup\n"
        ),
    )
    parser.add_argument("target", nargs="?", help="Local file, local folder, or remote URL")
    parser.add_argument("-d", dest="folder", default="", help="Destination folder on UC Files")
    parser.add_argument("--key", dest="key", default=None, help="Override API key for this session")
    parser.add_argument("--key-file", default=KEY_FILE, help="Path to API key file")
    parser.add_argument("--log-file", default=LOG_FILE, help="Path to log file")
    parser.add_argument(
        "--resume-dir", default=RESUME_DIR, help="Directory for folder-resume state files"
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {VERSION}")
    resume_group = parser.add_mutually_exclusive_group()
    resume_group.add_argument(
        "-r",
        "--resume",
        action="store_true",
        default=True,
        help="Resume previous upload state (default)",
    )
    resume_group.add_argument(
        "--no-resume", action="store_false", dest="resume", help="Do not resume; start fresh"
    )
    args = parser.parse_args()

    LOG_FILE = args.log_file
    KEY_FILE = args.key_file
    RESUME_DIR = args.resume_dir or tempfile.gettempdir()

    if not args.target:
        parser.print_help()
        sys.exit(0)

    # set up session first so we can validate the key
    session = make_session()

    # load or prompt for key
    API_KEY = load_key(args.key)
    if not API_KEY:
        API_KEY = prompt_key()

    if not validate_key(session, API_KEY):
        print("Invalid API key. Please check and try again.")
        sys.exit(1)

    # save key: always persist a validated key, whether from --key or from first prompt
    key_path = Path(os.path.expanduser(KEY_FILE))
    if args.key or not key_path.exists():
        save_key(API_KEY)
        print(f"`{key_path.resolve()}`")

    target = args.target
    folder = args.folder

    # detect mode
    is_url = target.startswith("http://") or target.startswith("https://")
    mode = "URL" if is_url else ("FOLDER" if os.path.isdir(target) else "FILE")
    log(f"=== SESSION START === target={target!r} folder={folder!r} mode={mode}")

    if is_url:
        process_url(session, target, folder)
    elif os.path.isdir(target):
        process_folder(session, target, folder, resume=args.resume)
    elif os.path.isfile(target):
        if folder:
            ensure_folder_exists(session, folder)
        process_file(session, target, folder)
    else:
        print(f"Target not found: {target}")
        sys.exit(1)

    print("")


if __name__ == "__main__":
    if sys.platform == "win32":
        import ctypes

        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass

    try:
        main()
    except KeyboardInterrupt:
        with progress_lock:
            pass
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    except Exception as exc:
        log(f"Unhandled exception: {exc}")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
