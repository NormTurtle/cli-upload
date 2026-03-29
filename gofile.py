#!/usr/bin/env python3
import argparse
import base64
import concurrent.futures
import contextlib
import hashlib
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# --- DEFAULTS ---
DEFAULT_ACCOUNT_ID = "926d12a4-9287-437d-8579-35ed1d800761"
DEFAULT_TOKEN = "PASTE_TOKEN_HERE"
LOG_FILE = ".gofile_upload.log"  # default is .gofile_upload.log
KEY_FILE = "~/.gofile_key"  # default is ~/.gofile_key
RESUME_DIR = None  # default to /tmp, dir to store uploaded file name/count
DEFAULT_UPLOAD_HOST = "upload.gofile.io"
FALLBACK_UPLOAD_HOSTS = [
    "upload.gofile.io",
    "upload-eu-par.gofile.io",
    "upload-na-phx.gofile.io",
    "upload-ap-sgp.gofile.io",
    "upload-ap-hkg.gofile.io",
    "upload-ap-tyo.gofile.io",
    "upload-sa-sao.gofile.io",
]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
MAX_RETRIES = 5
DEFAULT_FILE_THREADS = 3
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024  # 8MB streaming write size

# --- STATE ---
progress_lock = threading.Lock()
total_bytes_sent = 0
start_time = time.time()


def log(msg):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def human_size(n):
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} EB"


def _resume_state_path(folder_path, target_folder_name):
    global RESUME_DIR
    raw = f"{os.path.abspath(folder_path)}|{target_folder_name}"
    key = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    base_dir = RESUME_DIR if RESUME_DIR else tempfile.gettempdir()
    return Path(base_dir) / f".gofile_resume_{key}.json"


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
    except Exception as e:
        log(f"Resume state read failed ({state_path}): {e}")
        return {"version": 1, "done": {}}


def _save_resume_state(state_path, state):
    try:
        tmp = state_path.with_suffix(state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(state_path)
    except Exception as e:
        log(f"Resume state write failed ({state_path}): {e}")


def api_call(path, token, method="GET", data=None, retries=MAX_RETRIES):
    url = f"https://api.gofile.io{path}"
    headers = {"Authorization": f"Bearer {token}", "User-Agent": UA}
    payload = None

    if data is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(data).encode("utf-8")

    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=60) as res:
                body = res.read().decode().strip()
                parsed = json.loads(body) if body else {}
                if isinstance(parsed, dict) and parsed.get("status") == "ok":
                    return parsed
                if isinstance(parsed, dict) and parsed.get("status") == "error":
                    raise RuntimeError(parsed.get("message", "API error"))
                return parsed
        except Exception as e:
            log(f"API {method} {path} attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                time.sleep(min(2 * attempt, 10))

    return {}


def get_account_id(token, provided_account_id=""):
    if provided_account_id:
        return provided_account_id
    res = api_call("/accounts/getid", token)
    return (res.get("data") or {}).get("id") if isinstance(res, dict) else None


def get_root_folder(token, account_id):
    res = api_call(f"/accounts/{account_id}", token)
    return (res.get("data") or {}).get("rootFolder") if isinstance(res, dict) else None


def create_folder(token, parent_id, folder_name, is_public=None):
    data = {"parentFolderId": parent_id, "folderName": folder_name}
    if is_public is not None:
        data["public"] = bool(is_public)

    res = api_call("/contents/createFolder", token, method="POST", data=data)
    if not isinstance(res, dict):
        return None
    return (res.get("data") or {}).get("id")


def draw_progress(total_size):
    spin = ["|", "/", "-", "\\"]
    idx = 0
    while total_bytes_sent < total_size:
        with progress_lock:
            curr = total_bytes_sent

        pct = (curr / total_size) * 100 if total_size else 100
        elapsed = time.time() - start_time
        speed = curr / elapsed if elapsed > 1 else 0
        eta = (total_size - curr) / speed if speed > 0 else 0

        bar_w = 40
        filled = int((curr / total_size) * bar_w) if total_size else bar_w
        bar = "#" * max(0, filled - 1) + ">" + "-" * max(0, bar_w - filled)
        if curr >= total_size:
            bar = "#" * bar_w

        sys.stdout.write(
            f"\r{spin[idx % len(spin)]} {pct:6.2f}% [{bar[:bar_w]}] {human_size(curr)} {human_size(speed)}/s ETA {int(eta)}s"
        )
        sys.stdout.flush()
        idx += 1
        time.sleep(0.2)


def _build_multipart_headers(boundary, folder_id, filename):
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="folderId"\r\n\r\n'
        f"{folder_id}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()


def _upload_to_host(host, token, filepath, folder_id, chunk_size):
    global total_bytes_sent

    filename = os.path.basename(filepath)
    file_size = os.path.getsize(filepath)
    boundary = f"----GofileBoundary{int(time.time() * 1000)}"
    head = _build_multipart_headers(boundary, folder_id, filename)
    tail = f"\r\n--{boundary}--\r\n".encode()

    conn = http.client.HTTPSConnection(host, timeout=300)
    try:
        conn.connect()
        if conn.sock:
            conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        conn.putrequest("POST", "/uploadfile")
        conn.putheader("Authorization", f"Bearer {token}")
        conn.putheader("User-Agent", UA)
        conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length", str(len(head) + file_size + len(tail)))
        conn.endheaders()

        conn.send(head)
        with open(filepath, "rb") as f:
            while True:
                payload = f.read(chunk_size)
                if not payload:
                    break
                conn.send(payload)
                with progress_lock:
                    total_bytes_sent += len(payload)

        conn.send(tail)

        res = conn.getresponse()
        body = res.read().decode().strip()
        if res.status >= 400:
            raise RuntimeError(f"HTTP {res.status} from {host}: {body[:300]}")

        data = json.loads(body) if body else {}
        if data.get("status") != "ok":
            raise RuntimeError(f"Upload error on {host}: {data}")

        return data
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def upload_file(filepath, folder_id, token, hosts, chunk_size, verbose=False):
    file_size = os.path.getsize(filepath)

    for attempt in range(1, MAX_RETRIES + 1):
        for host in hosts:
            try:
                if verbose:
                    log(f"UPLOAD {filepath} via {host} attempt {attempt}")
                result = _upload_to_host(host, token, filepath, folder_id, chunk_size)
                link = (result.get("data") or {}).get("downloadPage")
                if link:
                    sys.stdout.write("\r" + " " * 140 + "\r")
                    print(
                        f"{os.path.basename(filepath)} [{human_size(file_size)}]\n{link}\n{base64.b64encode(link.encode()).decode()}\n{'-' * 48}"
                    )
                    return True
                log(f"No downloadPage in response for {filepath}: {result}")
            except Exception as e:
                log(f"Upload failed for {filepath} on {host} attempt {attempt}: {e}")

        time.sleep(min(2 * attempt, 10))

    sys.stdout.write("\r" + " " * 140 + "\r")
    print(f"FAILED: {os.path.basename(filepath)} [{human_size(file_size)}]")
    return False


def iter_upload_entries(target_path):
    if os.path.isfile(target_path):
        yield target_path, None
        return

    base_dir = os.path.normpath(os.path.abspath(target_path))
    for root, _, files in os.walk(base_dir):
        for filename in files:
            full = os.path.join(root, filename)
            # Directory uploads keep exact local tree structure on remote.
            yield full, os.path.normpath(os.path.abspath(root))


def build_remote_folder_tree(token, root_folder_id, target_dir, set_public=None):
    base_dir = os.path.normpath(os.path.abspath(target_dir))
    root_remote_id = create_folder(token, root_folder_id, os.path.basename(base_dir), set_public)
    if not root_remote_id:
        raise RuntimeError("Failed to create top-level remote folder")

    folder_map = {base_dir: root_remote_id}

    for root, dirs, _ in os.walk(base_dir):
        curr_abs = os.path.normpath(os.path.abspath(root))
        parent_remote_id = folder_map[curr_abs]
        for d in dirs:
            local_abs = os.path.normpath(os.path.abspath(os.path.join(root, d)))
            created = create_folder(token, parent_remote_id, d, set_public)
            if not created:
                raise RuntimeError(f"Failed to create remote folder for {local_abs}")
            folder_map[local_abs] = created

    return folder_map


def compute_total_size(target_path):
    total = 0
    for fp, _ in iter_upload_entries(target_path):
        with contextlib.suppress(FileNotFoundError):
            total += os.path.getsize(fp)
    return total


def normalize_hosts(primary_host, include_fallbacks):
    hosts = []

    p = primary_host.strip()
    if p:
        hosts.append(p)

    if include_fallbacks:
        for h in FALLBACK_UPLOAD_HOSTS:
            if h not in hosts:
                hosts.append(h)

    return hosts or [DEFAULT_UPLOAD_HOST]


def main():
    global start_time

    parser = argparse.ArgumentParser(description="Upload file/folder to GoFile")
    parser.add_argument("target", help="File or folder to upload")
    parser.add_argument("--token", default=None, help="GoFile API token")
    parser.add_argument("--account-id", default=None, help="GoFile account ID")
    parser.add_argument("--upload-host", default=DEFAULT_UPLOAD_HOST, help="Primary upload host")
    parser.add_argument(
        "--host-failover",
        action="store_true",
        help="Try other GoFile regional upload hosts if primary host fails",
    )
    parser.add_argument("--file-threads", type=int, default=DEFAULT_FILE_THREADS)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument(
        "--public",
        choices=["inherit", "true", "false"],
        default="inherit",
        help="Set created folders public/private, or inherit from parent",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--key-file", default=KEY_FILE, help="Path to API key file")
    parser.add_argument("--log-file", default=LOG_FILE, help="Path to log file")
    parser.add_argument(
        "--resume-dir", default=RESUME_DIR, help="Directory for folder-resume state files"
    )
    args = parser.parse_args()

    global LOG_FILE, KEY_FILE, RESUME_DIR
    LOG_FILE = args.log_file
    KEY_FILE = args.key_file
    RESUME_DIR = args.resume_dir or tempfile.gettempdir()

    key_path = os.path.expanduser(KEY_FILE)
    if args.token is None:
        if os.path.exists(key_path):
            with open(key_path) as f:
                args.token = f.read().strip()
        else:
            args.token = DEFAULT_TOKEN
            with open(key_path, "w") as f:
                f.write(DEFAULT_TOKEN)

    if args.account_id is None:
        args.account_id = DEFAULT_ACCOUNT_ID

    target = os.path.normpath(os.path.abspath(args.target))
    if not os.path.exists(target):
        sys.exit("Target does not exist")

    if args.file_threads < 1:
        sys.exit("--file-threads must be >= 1")
    if args.chunk_size < 64 * 1024:
        sys.exit("--chunk-size too small; use at least 65536")

    public_flag = None
    if args.public == "true":
        public_flag = True
    elif args.public == "false":
        public_flag = False

    account_id = get_account_id(args.token, args.account_id)
    if not account_id:
        sys.exit("Unable to resolve account ID")

    root_id = get_root_folder(args.token, account_id)
    if not root_id:
        sys.exit("Unable to resolve root folder from account")

    hosts = normalize_hosts(args.upload_host, args.host_failover)

    remote_folder_map = {}
    if os.path.isdir(target):
        remote_folder_map = build_remote_folder_tree(args.token, root_id, target, public_flag)

    folder_name = None if os.path.isfile(target) else os.path.basename(os.path.normpath(target))
    state_path = _resume_state_path(target, folder_name or "file")
    resume_state = _load_resume_state(state_path)

    to_upload = []
    skipped_count = 0
    skipped_bytes = 0

    fail_count = 0
    for filepath, root_dir in iter_upload_entries(target):
        folder_id = remote_folder_map.get(root_dir) if os.path.isdir(target) else root_id

        if not folder_id:
            log(f"Missing folder mapping for {filepath}")
            fail_count += 1
            continue

        rel_file = os.path.relpath(filepath, target).replace("\\", "/")
        st = os.stat(filepath)
        size = st.st_size
        mtime_ns = st.st_mtime_ns

        done_entry = resume_state["done"].get(rel_file)
        if (
            isinstance(done_entry, dict)
            and done_entry.get("size") == size
            and done_entry.get("mtime_ns") == mtime_ns
        ):
            skipped_count += 1
            skipped_bytes += size
            continue
        to_upload.append((filepath, folder_id, rel_file, size, mtime_ns))

    if not to_upload:
        print("No pending files found in the folder (resume checkpoint says all done).")
        sys.exit(0)

    total_size = sum(f[3] for f in to_upload)
    start_time = time.time()
    threading.Thread(target=draw_progress, args=(total_size,), daemon=True).start()

    ok_count = 0

    with ThreadPoolExecutor(max_workers=args.file_threads) as exe:
        inflight = {}

        for filepath, folder_id, rel_file, size, mtime_ns in to_upload:
            if len(inflight) >= args.file_threads * 3:
                done, _ = concurrent.futures.wait(
                    inflight.keys(), return_when=concurrent.futures.FIRST_COMPLETED
                )
                for fut in done:
                    info = inflight.pop(fut)
                    if fut.result():
                        ok_count += 1
                        resume_state["done"][info["rel_file"]] = {
                            "size": info["size"],
                            "mtime_ns": info["mtime_ns"],
                            "uploaded_at": int(time.time()),
                        }
                        _save_resume_state(state_path, resume_state)
                    else:
                        fail_count += 1

            fut = exe.submit(
                upload_file,
                filepath,
                folder_id,
                args.token,
                hosts,
                args.chunk_size,
                args.verbose,
            )
            inflight[fut] = {"rel_file": rel_file, "size": size, "mtime_ns": mtime_ns}

        for fut in concurrent.futures.as_completed(inflight):
            info = inflight.pop(fut)
            if fut.result():
                ok_count += 1
                resume_state["done"][info["rel_file"]] = {
                    "size": info["size"],
                    "mtime_ns": info["mtime_ns"],
                    "uploaded_at": int(time.time()),
                }
                _save_resume_state(state_path, resume_state)
            else:
                fail_count += 1

    sys.stdout.write("\r" + " " * 140 + "\r")
    print(
        f"Done. Success: {ok_count}, Failed: {fail_count}, Skipped: {skipped_count} "
        f"({human_size(skipped_bytes)})"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
