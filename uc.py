#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["requests", "rich"]
# ///

# --- CONFIG ---
API_KEY = "PASTE_API_KEY_HERE"  # ← paste your 32-char hash here or  ~/.uc_key
LOG_FILE = ".uc.log"  # default is .uc.log
KEY_FILE = "~/.uc_key"  # default is ~/.uc_key
RESUME_DIR = ""  # default to /tmp, dir to store uploaded file name/count
API_BASE = "https://files.union-crax.xyz"

# --- IMPORTS ---
# ruff: noqa: E402
import argparse
import contextlib
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

# Rich for UV-style UI
from rich.console import Console, Group
from rich.filesize import decimal
from rich.live import Live
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Column
from rich.text import Text

# --- INTERNAL CONFIG ---
try:
    from importlib.metadata import PackageNotFoundError, version

    try:
        VERSION = version("ucf")
    except PackageNotFoundError:
        import re

        try:
            _pyproject = Path(__file__).parent / "pyproject.toml"
            _match = re.search(
                r'^version\s*=\s*["\']([^"\']+)["\']',
                _pyproject.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
            VERSION = _match.group(1) if _match else "0.0.0-dev"
        except Exception:
            VERSION = "0.0.0-dev"
except ImportError:
    VERSION = "0.0.0-dev"

FILE_THREADS = 10  # base concurrency
CHUNK_THREADS = 4  # base chunks
DOWNLOAD_CONNS = 16  # parallel Range connections when downloading a URL
CHUNK_SIZE = 50 * 1024 * 1024  # optimistic default; auto-lowered if server rejects
MINI_CHUNK_SIZE = 1024 * 1024  # 1MB read buffer for 10Gbps throughput
MAX_SIMPLE_SIZE = 50 * 1024 * 1024  # files <= this go through simple upload
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds between retries

# --- STATE ---
progress_lock = threading.Lock()
log_lock = threading.Lock()
active_uploads = {}
atomic_bytes_done = 0  # True uploaded byte count
last_ui_update_bytes = 0  # For throttling
is_folder_mode = False
progress_active = False
EXPIRY_MINUTES = 0  # default: no expiry

# Respect NO_COLOR
NO_COLOR = "NO_COLOR" in os.environ
console = Console(no_color=NO_COLOR)

known_folders = set()  # folders we've already verified/created this run
_folders_fetched = False
_detected_chunk_limit_bytes = None
_detected_chunk_limit_lock = threading.Lock()


class AverageSpeedColumn(ProgressColumn):
    """Renders the true average network speed, smoothing out SSD burst reads."""

    def render(self, task):
        if task.total is None or task.elapsed is None or task.elapsed < 0.1:
            return Text("? MB/s", style="progress.data.speed", justify="right")

        # True average speed: total bytes / total seconds
        avg_speed = task.completed / task.elapsed
        speed_str = f"{decimal(int(avg_speed))}/s"
        return Text(speed_str, style="progress.data.speed", justify="right")


# Global Rich Progress Setup
overall_progress = Progress(
    SpinnerColumn(),
    TextColumn("{task.description}"),
    TimeElapsedColumn(),
    AverageSpeedColumn(),
    TextColumn("{task.fields[skip_text]}"),
    console=console,
)


def truncate_middle(text, max_length=75):
    """Truncate text in the middle to preserve start and end (e.g., file extensions)."""
    if len(text) <= max_length:
        return text.ljust(max_length)
    half = (max_length - 3) // 2
    return text[: half + (max_length - 3) % 2] + "..." + text[-half:]


# Custom Bar column configured to stretch and look like uv
uv_bar = BarColumn(
    bar_width=None, complete_style="green", finished_style="dim", pulse_style="dim white"
)
uv_bar.complete_char = "#"
uv_bar.finished_char = "-"
uv_bar.remaining_char = "-"

file_progress = Progress(
    TextColumn(
        "[cyan]{task.fields[display_name]}[/cyan]", table_column=Column(width=75, no_wrap=True)
    ),
    uv_bar,
    DownloadColumn(table_column=Column(justify="right", width=17)),
    AverageSpeedColumn(table_column=Column(justify="right", width=12)),
    console=console,
    expand=True,
)

progress_group = Group(overall_progress, file_progress)

rich_live = None
task_ids = {}
total_task_id = None


# --- SETUP (key loading, session creation) ---


def fetch_limits(session, is_premium=False):
    """Fetch public upload limits from the API and adjust local constants dynamically."""
    global MAX_SIMPLE_SIZE, CHUNK_SIZE, CHUNK_THREADS, FILE_THREADS
    try:
        data = api_request(session, "GET", "/api/limits")
        # Balanced scaling: avoid overwhelming the server finalization queue
        if data.get("ok"):
            limit = data.get("max_upload_size", data.get("max_simple_size", MAX_SIMPLE_SIZE))
            MAX_SIMPLE_SIZE = limit
            if is_premium:
                CHUNK_SIZE = min(limit, 100 * 1024 * 1024)
                CHUNK_THREADS = 4
                FILE_THREADS = 4  # Server hard-caps at ~1.1 files/sec. Higher = connection drops.
            else:
                CHUNK_SIZE = min(limit, 50 * 1024 * 1024)
                CHUNK_THREADS = 2
                FILE_THREADS = 2

            log(
                f"Dynamic limits: MAX_SIMPLE_SIZE={human_size(MAX_SIMPLE_SIZE)}, "
                f"FILE_THREADS={FILE_THREADS}, CHUNK_THREADS={CHUNK_THREADS}"
            )
    except Exception as exc:
        log(f"Could not fetch limits, using defaults: {exc}")


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
        console.print("[red]No key provided. Exiting.[/red]")
        sys.exit(1)
    return key


def validate_key(session, key):
    """Check the key against GET /api/auth/me. Returns account info dict if valid, else None."""
    try:
        resp = session.get(
            f"{API_BASE}/api/auth/me",
            headers={"X-API-Key": key},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok"):
                return data
        if resp.status_code == 401:
            return None
        # treat other errors as validation failure
        log(f"Key validation returned status {resp.status_code}: {resp.text}")
        return None
    except requests.RequestException as exc:
        log(f"Key validation network error: {exc}")
        return None


def save_key(key):
    """Persist a validated key to ~/.uc_key with restrictive permissions."""
    key_path = Path(os.path.expanduser(KEY_FILE))
    key_path.write_text(key)
    with contextlib.suppress(OSError):  # Windows doesn't support Unix permissions
        key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600 — owner-only


def make_session():
    """Build a requests.Session with large connection pools for threading."""
    session = requests.Session()
    # pool_maxsize should handle massive parallel file uploads to beat server TTFB latency
    adapter = HTTPAdapter(pool_connections=200, pool_maxsize=400, max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers["User-Agent"] = "uc-cli/1.0"
    return session


def auth_headers():
    """Return the auth header dict. Every request needs this."""
    return {"X-API-Key": API_KEY}


# --- UTILITIES (log, human_size, human_time, etc.) ---

# ANSI Colors
GOLD = "\033[38;2;251;191;36m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
RED = "\033[91m"
FADED = "\033[90m"
RESET = "\033[0m"


def colorize(text, color_code):
    """Wrap text in ANSI color codes if stdout is a TTY."""
    if NO_COLOR or not sys.stdout.isatty():
        return text
    return f"{color_code}{text}{RESET}"


def format_duration(seconds):
    """Format time: '20 minute', '1:30 hour', or '0.4 second'."""
    if seconds < 1:
        return f"{seconds:.1f} second"
    if seconds < 60:
        return f"{int(seconds)} second"
    if seconds < 3600:
        mins = int(seconds // 60)
        return f"{mins} minute"

    hours = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    if mins > 0:
        return f"{hours}:{mins:02d} hour"
    return f"{hours} hour"


def format_completion(size_bytes, elapsed_seconds):
    """Format done message: '51.83KB in 8ms'."""
    size_str = human_size(size_bytes).replace(" ", "")

    if elapsed_seconds < 1:
        ms = int(elapsed_seconds * 1000)
        time_str = f"{ms}ms"
    elif elapsed_seconds < 60:
        time_str = f"{elapsed_seconds:.1f}s"
    else:
        time_str = format_duration(elapsed_seconds).replace(" ", "")

    return f"{size_str} in {time_str}"


class ColorHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """A custom formatter to add colors to the help message."""

    def _format_usage(self, usage, actions, groups, prefix):
        prefix = colorize("usage: ", BLUE) if prefix is None else colorize(prefix, BLUE)

        usage_str = super()._format_usage(usage, actions, groups, prefix)

        # Colorize the program name
        prog = self._prog
        usage_str = usage_str.replace(prog, colorize(prog, MAGENTA), 1)

        # Colorize arguments in brackets [args]
        usage_str = re.sub(r"(\[.*?\])", lambda m: colorize(m.group(1), CYAN), usage_str)
        return usage_str

    def start_section(self, heading):
        return super().start_section(colorize(heading, BLUE))

    def _format_action_invocation(self, action):
        if not action.option_strings:
            (metavar,) = self._metavar_formatter(action, action.dest)(1)
            return colorize(metavar, GREEN)
        else:
            parts = []
            if action.nargs == 0:
                parts.extend([colorize(s, GREEN) for s in action.option_strings])
            else:
                default = action.dest.upper()
                args_string = self._format_args(action, default)
                for option_string in action.option_strings:
                    parts.append(
                        f"{colorize(option_string, GREEN)} {colorize(args_string, YELLOW)}"
                    )
            return ", ".join(parts)


def log(msg):
    """Append a timestamped, thread-aware message to .uc.log cleanly across threads."""
    try:
        t = time.time()
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
        ms = int((t % 1) * 1000)
        tid = threading.get_ident()
        with log_lock, open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}.{ms:03d}] [T-{tid}] {msg}\n")
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
    """Make an API request. Optimized for high-throughput Turbo Mode."""
    url = f"{API_BASE}{path}"
    kwargs.setdefault("headers", {})
    kwargs["headers"].update(auth_headers())
    kwargs.setdefault("timeout", 60)

    for attempt in range(1, retries + 1):
        try:
            resp = session.request(method, url, **kwargs)

            if resp.status_code == 401:
                if attempt < retries:
                    time.sleep(RETRY_DELAY)
                    continue
                console.print("\n[red]Invalid API key — delete ~/.uc_key and re-run.[/red]")
                os._exit(1)

            if resp.status_code == 413:
                raise requests.HTTPError("413 Payload Too Large", response=resp)

            if resp.status_code >= 500 and attempt < retries:
                time.sleep(min(10, RETRY_DELAY * attempt))
                continue

            # Performance: Parse JSON exactly once and return the object
            if resp.headers.get("content-type", "").startswith("application/json"):
                try:
                    data = resp.json()
                    if not data.get("ok") and resp.status_code == 200:
                        raise Exception(f"API Error: {data.get('error', 'Unknown')}")
                    return data
                except ValueError:
                    return resp

            return resp

        except requests.RequestException as exc:
            if attempt < retries:
                # Connection dropped or timed out — retry quickly
                # (likely a stale keep-alive)
                delay = (
                    1
                    if isinstance(
                        exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
                    )
                    else RETRY_DELAY
                )
                time.sleep(delay)
                # Rewind stream if possible for retry
                if "data" in kwargs and hasattr(kwargs["data"], "seek"):
                    with contextlib.suppress(Exception):
                        kwargs["data"].seek(0)
                continue
            raise exc


# --- PROGRESS BAR ---


def start_progress(filename, total_size, folder_mode=False):
    global progress_active, is_folder_mode, progress_group, rich_live, task_ids, file_progress
    with progress_lock:
        is_folder_mode = folder_mode
        if not progress_active:
            progress_active = True
            if not rich_live:
                rich_live = Live(
                    progress_group, console=console, refresh_per_second=10, transient=False
                )
                rich_live.start()

        # Always add individual tasks to file_progress now, since we immediately remove them
        display_name = truncate_middle(os.path.basename(filename), 75)
        task_id = file_progress.add_task("upload", total=total_size, display_name=display_name)
        task_ids[filename] = task_id

        active_uploads[filename] = {
            "done": 0,
            "total": total_size,
            "start_time": time.time(),
        }


def finish_progress(filename, size, url, elapsed=None, speed=None):
    global progress_active, file_progress, rich_live, task_ids
    with progress_lock:
        # Stop and remove individual task immediately to avoid clutter
        if filename in task_ids:
            task_id = task_ids[filename]
            file_progress.update(task_id, visible=False)
            file_progress.remove_task(task_id)
            del task_ids[filename]

        # Record timing for stats
        stats = active_uploads.get(filename, {"start_time": time.time()})
        dur = time.time() - stats["start_time"]

        # SILENT SUCCESS: Only print in single-file mode
        if not is_folder_mode:
            msg = Text.assemble(
                (" ", ""),
                (f"{ok_symbol()} ", "green"),
                (f"{filename} ", "cyan"),
                (f"{human_size(size)} ", "dim"),
                (f"in {format_duration(dur)}", "dim"),
            )
            console.print(msg)
            if url:
                if not url.startswith("http"):
                    url = f"{API_BASE}{url}"
                console.print(f"   [cyan]{url}[/cyan]")

        if filename in active_uploads:
            del active_uploads[filename]

        if not active_uploads and progress_active and not is_folder_mode:
            if rich_live:
                rich_live.stop()
                rich_live = None
            progress_active = False


def fail_progress(filename, exc):
    global progress_active, file_progress, rich_live, task_ids
    with progress_lock:
        if filename in task_ids:
            task_id = task_ids[filename]
            file_progress.remove_task(task_id)
            del task_ids[filename]

        if filename in active_uploads:
            del active_uploads[filename]

        console.print(f" [red][X] FAIL: {filename}[/red] -> {exc}")

        if not active_uploads and progress_active and not is_folder_mode:
            if rich_live:
                rich_live.stop()
                rich_live = None
            progress_active = False


def add_progress(filename, n):
    global \
        atomic_bytes_done, \
        last_ui_update_bytes, \
        overall_progress, \
        file_progress, \
        task_ids, \
        total_task_id
    # update atomic counter
    with progress_lock:
        atomic_bytes_done += n
        current_total = atomic_bytes_done

        # Throttle UI updates for the overall task to save CPU
        if (
            overall_progress
            and total_task_id is not None
            and (current_total - last_ui_update_bytes) > (512 * 1024)
        ):
            overall_progress.update(total_task_id, completed=current_total)
            last_ui_update_bytes = current_total

    # Individual file bars are updated immediately (they handle their own frame limiting in Rich)
    if file_progress and filename in task_ids:
        file_progress.update(task_ids[filename], advance=n)


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
            data = api_request(session, "GET", "/api/folders")
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
            rdata = api_request(
                session,
                "POST",
                "/api/folders/create",
                json={"name": folder_name},
            )
            known_folders.add(folder_name)
            # capture folder url from creation response
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


def _build_multipart(filename, filepath, file_size, folder="", expires_minutes=0):
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
    # optional expiry
    if expires_minutes > 0:
        parts_header += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="expires_minutes"'
            f"\r\n\r\n{expires_minutes}\r\n"
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
        open(filepath, "rb"),  # noqa: SIM115
        parts_footer,
        total_body,
        file_size,
        filename,
    )
    return boundary, stream, total_body


class _BoundedFile:
    def __init__(self, filepath, offset, length):
        self.f = open(filepath, "rb")  # noqa: SIM115
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
        self._progress_buffer = 0

    def _flush_progress(self):
        if self._progress_buffer > 0:
            add_progress(self._filename, self._progress_buffer)
            self._progress_buffer = 0

    def read(self, n=-1):
        """Read n bytes across parts, updating progress for file content bytes."""
        if n is None or n < 0:
            result = b"".join(p.read() for p in self._parts[self._idx :])
            self._idx = len(self._parts)
            file_bytes = max(0, len(result) - max(0, self._header_len - self._bytes_read))
            if file_bytes > 0:
                self._progress_buffer += min(file_bytes, self._file_size)
                if self._progress_buffer >= 512 * 1024:
                    self._flush_progress()
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
                    self._progress_buffer += prog
                    # ⚡ Bolt: Batch global progress updates to reduce lock contention
                    if self._progress_buffer >= 512 * 1024:
                        self._flush_progress()
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
        file_progress_reported -= self._progress_buffer
        self._progress_buffer = 0

        if file_progress_reported > 0:
            add_progress(self._filename, -file_progress_reported)
        self._bytes_read = 0

    def close(self):
        self._flush_progress()
        for p in self._parts:
            with contextlib.suppress(Exception):
                p.close()


def upload_small(session, filepath, folder="", folder_mode=False, filename=None):
    """Upload a file <= 50 MB using the simple POST /api/upload endpoint."""
    global EXPIRY_MINUTES
    if filename is None:
        filename = os.path.basename(filepath)
    file_size = os.path.getsize(filepath)

    log(
        f"UPLOAD SMALL: {filename} ({human_size(file_size)}) -> "
        f"folder={folder!r} expiry={EXPIRY_MINUTES}"
    )
    start_progress(filename, file_size, folder_mode)
    t0 = time.time()
    try:
        # Stream the upload so the rich progress bar updates in real-time
        boundary, stream, body_len = _build_multipart(
            filename, filepath, file_size, folder, EXPIRY_MINUTES
        )
        try:
            result = api_request(
                session,
                "POST",
                "/api/upload",
                data=stream,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(body_len),
                },
                retries=3,
                timeout=300,
            )
        finally:
            stream.close()

        elapsed = time.time() - t0
        avg_speed = file_size / elapsed if elapsed > 0 else 0

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
    global EXPIRY_MINUTES
    chunk_size = _get_effective_chunk_size()

    for _attempt in range(2):
        chunk_count = math.ceil(file_size / chunk_size)
        data = api_request(
            session,
            "POST",
            "/api/upload/init",
            json={
                "filename": filename,
                "size": file_size,
                "chunk_count": chunk_count,
                "folder": folder,
                "expires_minutes": EXPIRY_MINUTES,
            },
        )

        # api_request returns the dict if ok is true
        if isinstance(data, dict) and data.get("ok"):
            upload_id = data.get("upload_id", data.get("uploadId", ""))
            if upload_id:
                return upload_id, chunk_size, chunk_count, data
            raise RuntimeError(f"Chunk init missing upload_id: {data}")

        # Fallback if api_request returned raw response (for status code checking)
        if hasattr(data, "status_code"):
            body = (data.text or "").strip()
            limit_bytes = _extract_chunk_limit_bytes(body)
            if data.status_code == 400 and limit_bytes:
                _set_detected_chunk_limit(limit_bytes)
                if limit_bytes != chunk_size:
                    chunk_size = limit_bytes
                    continue
            raise RuntimeError(f"Chunk init failed ({data.status_code}): {body[:300]}")

    raise RuntimeError("Chunk init failed after adaptive retry")


def upload_large(session, filepath, folder="", folder_mode=False, filename=None):
    """Upload a file > 50 MB using chunked init → chunk uploads → finish → poll."""
    if filename is None:
        filename = os.path.basename(filepath)
    file_size = os.path.getsize(filepath)

    # step 1: init the chunked upload
    try:
        upload_id, chunk_size, chunk_count, _init_data = _init_chunked_upload(
            session, filename, file_size, folder
        )
    except Exception as exc:
        log(f"Chunked init exception for {filename}: {exc}")
        console.print(f"[red][X] FAIL: {filename}[/red] -> chunked init failed: {exc}")
        return None

    log(
        f"UPLOAD LARGE: {filename} ({human_size(file_size)}) -> "
        f"{chunk_count} chunks @ {human_size(chunk_size)}, folder={folder!r}"
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
    finish_data = api_request(
        session,
        "POST",
        "/api/upload/finish",
        json={"upload_id": upload_id},
    )

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
                data = api_request(session, "GET", f"/api/upload/status?id={status_id}", retries=1)

                # api_request returns dict if success
                if isinstance(data, dict):
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
        elif file_size:
            # test for range support with a 1-byte request
            try:
                r_test = session.get(url, headers={"Range": "bytes=0-0"}, timeout=5, stream=True)
                r_test.close()
                if r_test.status_code == 206:
                    supports_range = True
            except Exception:
                pass

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
        console.print(f"\n[red]Chunked upload init failed for {filename}: {exc}[/red]")
        return None

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
    finish_data = api_request(
        session,
        "POST",
        "/api/upload/finish",
        json={"upload_id": upload_id},
    )

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
    aria2_success = False

    import subprocess

    # Attempt ultra-fast aria2 download if uvx is available and starting fresh
    if existing_size == 0 and shutil.which("uvx"):
        try:
            cmd = [
                "uvx",
                "--from",
                "aria2",
                "aria2c",
                "-c",
                "-x16",
                "-s16",
                "-k1M",
                "--min-split-size=1M",
                "--file-allocation=none",
                "-d",
                tmp_dir,
                "-o",
                safe_name,
                url,
            ]
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res.returncode == 0 and os.path.exists(tmp_path):
                aria2_success = True
                downloaded_bytes = os.path.getsize(tmp_path)
                add_progress(dl_name, downloaded_bytes)
        except Exception as exc:
            log(f"aria2 fallback failed: {exc}")

    if not aria2_success:
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
    process_file(session, tmp_path, folder, folder_mode, filename)

    # clean up temp file
    with contextlib.suppress(OSError):
        os.remove(tmp_path)


# --- ORCHESTRATOR (process_file, process_url, process_folder) ---


def process_file(session, filepath, folder="", folder_mode=False, filename=None):
    """Decide whether to use small or chunked upload for a local file."""
    file_size = os.path.getsize(filepath)
    if folder:
        ensure_folder_exists(session, folder)
    if file_size <= MAX_SIMPLE_SIZE:
        return upload_small(session, filepath, folder, folder_mode, filename)
    else:
        return upload_large(session, filepath, folder, folder_mode, filename)


def process_folder(session, folder_path, dest_folder="", resume=True):
    """Walk a local folder and upload every file one at a time, preserving subfolder structure."""
    folder_path = os.path.normpath(folder_path)
    base_name = os.path.basename(folder_path)
    target_folder = dest_folder if dest_folder else base_name

    # persistent checkpoint so retries continue where they left off
    state_path = _resume_state_path(folder_path, target_folder)
    resume_state = _load_resume_state(state_path) if resume else {"version": 1, "done": {}}

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
                    remote_folder = f"{dest_folder}/{base_name}".replace("\\", "/")
                else:
                    remote_folder = f"{dest_folder}/{base_name}/{rel_dir}".replace("\\", "/")
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
        console.print(
            "[dim]No pending files found in the folder (resume checkpoint says all done).[/dim]"
        )
        return

    total_size = sum(f[3] for f in to_upload)

    # Print intent immediately
    console.print(
        f"Uploading pending [cyan]{len(to_upload)}[/cyan] file(s) "
        f"({human_size(total_size)}), skipping [dim]{skipped_count}[/dim] "
        f"already-completed file(s) ({human_size(skipped_bytes)})"
    )
    console.print("")

    # start uploads in massive batches
    t0_folder = time.time()
    success_count = 0
    fail_count = 0

    global total_task_id
    # Add Total Progress anchor
    skip_str = (
        f", skipping {skipped_count} already-completed file(s) ({human_size(skipped_bytes)})"
        if skipped_count > 0
        else ""
    )
    total_task_id = overall_progress.add_task(
        f"Uploading 0/{len(to_upload)} ({human_size(total_size)})",
        total=total_size,
        skip_text=skip_str,
    )

    # Ensure thread pool is large enough for premium concurrency
    # Start Live display context IMMEDIATELY to show activity
    with (
        ThreadPoolExecutor(max_workers=FILE_THREADS) as pool,
        Live(progress_group, console=console, refresh_per_second=10, transient=False) as live,
    ):
        global rich_live
        rich_live = live
        futures = {}
        for path, remote_dir, rel_file, size, mtime_ns in to_upload:
            # Pre-submit tasks as fast as possible.
            # ensure_folder_exists is called inside process_file on-demand.
            fut = pool.submit(process_file, session, path, remote_dir, True)
            futures[fut] = (rel_file, size, mtime_ns)

        # Process results as they complete, using immediate UI updates
        for fut in as_completed(futures):
            rel_file, size, mtime_ns = futures[fut]
            try:
                result = fut.result()
                if result:
                    success_count += 1
                    # Performance: Update local memory state ONLY
                    resume_state["done"][rel_file] = {
                        "size": size,
                        "mtime_ns": mtime_ns,
                        "url": result,
                        "uploaded_at": int(time.time()),
                    }
                else:
                    fail_count += 1
            except Exception as exc:
                # Log failures to memory log if possible
                log(f"Critical upload error for {rel_file}: {exc}")
                console.print(f" [red][X] FAIL: {rel_file}[/red] -> {exc}")
                fail_count += 1

            # Update File Count in real time so tiny files don't look "stuck"
            if total_task_id is not None:
                task = overall_progress._tasks.get(total_task_id)
                if task:
                    processed = success_count + fail_count
                    if processed >= len(to_upload):
                        overall_progress.update(total_task_id, description="Finalizing...")
                    else:
                        desc = f"Uploading {processed}/{len(to_upload)} ({human_size(total_size)})"
                        overall_progress.update(total_task_id, description=desc)

        # Reset global UI states
        rich_live = None
        total_task_id = None

    # Turbo Mode: Save state EXACTLY ONCE at the end
    _save_resume_state(state_path, resume_state)

    # folder summary at the end
    elapsed = time.time() - t0_folder
    avg_speed = total_size / elapsed if elapsed > 0 else 0
    folder_link = folder_urls.get(target_folder, "")
    if folder_link and not folder_link.startswith("http"):
        folder_link = f"{API_BASE}{folder_link}"

    log(
        f"FOLDER DONE: {target_folder} "
        f"pending=[{human_size(total_size)}] "
        f"in {human_time(elapsed)} at {human_size(avg_speed)}/s, "
        f"success={success_count}, failed={fail_count}, "
        f"skipped={skipped_count}, "
        f"checkpoint={str(state_path)!r}, link={folder_link!r}"
    )

    summary = Text.assemble(
        (f"{target_folder} ", "cyan"),
        (f"[{human_size(total_size)} pending] - ", "dim"),
        (f"{format_duration(elapsed)} ", "green"),
        (f"(success={success_count}, failed={fail_count}, skipped={skipped_count})", "dim"),
    )
    console.print(summary)
    if folder_link:
        console.print(f"[cyan]{folder_link}[/cyan]")

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
        description=colorize(
            "uc.py — blazing-fast CLI uploader for UC Files (files.union-crax.xyz)",
            CYAN,
        ),
        formatter_class=ColorHelpFormatter,
        epilog=(
            f"{colorize('examples:', BLUE)}\n"
            f"  python {colorize('uc.py', MAGENTA)} video.mp4\n"
            f"  {colorize('uvx ucf', MAGENTA)} /home/user/movies -d Films\n"
            f"  {colorize('uvx ucf', MAGENTA)} https://example.com/archive.zip -d Downloads\n"
            f"  {colorize('uv run uc.py', MAGENTA)} big_folder/ -d Backup\n"
        ),
    )
    parser.add_argument("target", nargs="?", help="Local file, local folder, or remote URL")
    parser.add_argument("-d", dest="folder", default="", help="Destination folder on UC Files")
    parser.add_argument(
        "-e", "--expiry", type=int, default=0, help="File expiry in minutes (0 = never)"
    )
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
    global EXPIRY_MINUTES
    EXPIRY_MINUTES = args.expiry

    if not args.target:
        parser.print_help()
        sys.exit(0)

    # set up session first so we can validate the key
    session = make_session()

    # load or prompt for key
    API_KEY = load_key(args.key)
    was_prompted = False
    if not API_KEY:
        API_KEY = prompt_key()
        was_prompted = True

    account_info = validate_key(session, API_KEY)
    if not account_info:
        console.print("[red]Invalid API key. Please check and try again.[/red]")
        sys.exit(1)

    # save key: always persist a validated key, whether from --key or from first prompt
    key_path = Path(os.path.expanduser(KEY_FILE))
    if args.key or was_prompted or not key_path.exists() or key_path.stat().st_size == 0:
        save_key(API_KEY)
        console.print(f"[dim]Key verified and saved to `{key_path.resolve()}`[/dim]")

    # Fetch limits and show account info
    is_premium = account_info.get("is_premium", False)
    fetch_limits(session, is_premium)

    premium_label = "[gold1]Premium[/gold1]" if is_premium else "Free"
    login_msg = f"Logged in as: {account_info.get('hash')[:8]}... [{premium_label}]"
    console.print(f"[dim]{login_msg}[/dim]")

    file_count = account_info.get("file_count", 0)
    console.print(f"[dim]Files: {file_count}[/dim]")

    if EXPIRY_MINUTES > 0:
        console.print(f"Expiry set to: {EXPIRY_MINUTES} minutes", style="gold1")
    console.print("")

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
        console.print(f"[red]Target not found: {target}[/red]")
        sys.exit(1)

    console.print("")


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
        import traceback

        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
