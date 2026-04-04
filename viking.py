#!/usr/bin/env python3
import argparse
import base64
import concurrent.futures
import hashlib
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# --- CONFIG ---
DEFAULT_USER_HASH = "PASTE_USER_HASH_HERE"
LOG_FILE = ".viking_logs"  # default is .viking_logs
KEY_FILE = "~/.viking_key"  # default is ~/.viking_key
RESUME_DIR = None  # default to /tmp, dir to store uploaded file name/count
DEFAULT_PUBLIC_UPLOAD_URL = (
    "https://vikingfile.com/public-upload/F3lH1nC3TR9S9syBWbgSxmUmA1G2ord_Czvw4_pBSas"
)
API_BASE = "https://vikingfile.com/api"
FILE_THREADS = 4  # Files at once
CHUNK_THREADS = 4  # Chunks per large file
CHUNK_READ_SIZE = 4 * 1024 * 1024  # 4MB payload writes for better throughput
MAX_RETRIES = 5
FORCE_LEGACY_FOR_FOLDERS = True  # safer for remote folder-path creation in UI
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# --- STATE ---
progress_lock = threading.Lock()
total_bytes_sent = 0
start_time = time.time()


def log(msg):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")


def human_size(n):
    for u in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if n < 1024:
            return f"{n:.2f} {u}"
        n /= 1024
    return f"{n:.2f} EB"


def _resume_state_path(folder_path, target_folder_name):
    global RESUME_DIR
    raw = f"{os.path.abspath(folder_path)}|{target_folder_name}"
    key = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    base_dir = RESUME_DIR if RESUME_DIR else tempfile.gettempdir()
    return Path(base_dir) / f".viking_resume_{key}.json"


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


def extract_public_share_token(url_or_token):
    if not url_or_token:
        return ""
    value = url_or_token.strip()
    if "/public-upload/" not in value:
        return value
    parsed = urllib.parse.urlparse(value)
    marker = "/public-upload/"
    idx = parsed.path.find(marker)
    if idx == -1:
        return ""
    return parsed.path[idx + len(marker) :].strip("/")


def draw_progress(total_size):
    spin = ["|", "/", "-", "\\"]
    idx = 0
    while total_bytes_sent < total_size:
        curr = total_bytes_sent
        perc = (curr / total_size) * 100 if total_size else 100
        elap = time.time() - start_time
        speed = curr / elap if elap > 1 else 0
        eta = (total_size - curr) / speed if speed > 0 else 0
        bar_w = 50
        filled = int(bar_w * curr / total_size) if total_size else bar_w
        bar = "#" * max(0, filled - 1) + ">" + "-" * max(0, bar_w - filled)
        if curr >= total_size:
            bar = "#" * bar_w
        sys.stdout.write(
            f"\r{spin[idx % len(spin)]} {perc:6.2f}% [{bar[:bar_w]}] {human_size(curr)} {human_size(speed)}/s ETA {int(eta)}s "
        )
        sys.stdout.flush()
        idx += 1
        time.sleep(0.2)


def api(end, data=None, retries=MAX_RETRIES):
    url = f"{API_BASE}/{end}"
    headers = {"User-Agent": UA, "Referer": "https://vikingfile.com/"}
    req_data = urllib.parse.urlencode(data).encode() if data else None

    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=req_data, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as res:
                body = res.read().decode().strip()
                return json.loads(body) if body else {}
        except Exception as e:
            log(f"API Error {end} attempt {attempt}/{retries}: {e}")
            time.sleep(min(2 * attempt, 10))
    return {}


def upload_chunk(url, path, offset, size, p_num):
    """Native socket streaming for precise progress tracking with retries."""
    parsed = urllib.parse.urlparse(url)
    request_target = parsed.path or "/"
    if parsed.query:
        request_target += f"?{parsed.query}"

    for attempt in range(1, MAX_RETRIES + 1):
        conn = None
        try:
            conn = (
                http.client.HTTPSConnection(parsed.netloc, timeout=180)
                if parsed.scheme == "https"
                else http.client.HTTPConnection(parsed.netloc, timeout=180)
            )
            conn.putrequest("PUT", request_target)
            conn.putheader("Content-Length", str(size))
            conn.putheader("User-Agent", UA)
            conn.endheaders()

            with open(path, "rb") as f:
                f.seek(offset)
                sent = 0
                while sent < size:
                    payload = f.read(min(CHUNK_READ_SIZE, size - sent))
                    if not payload:
                        break
                    conn.send(payload)
                    sent += len(payload)
                    with progress_lock:
                        global total_bytes_sent
                        total_bytes_sent += len(payload)

            res = conn.getresponse()
            status = res.status
            etag = res.getheader("ETag", "").replace('"', "")
            _ = res.read()
            conn.close()

            if status not in (200, 201, 204):
                raise RuntimeError(f"chunk {p_num} HTTP {status}")
            if not etag:
                raise RuntimeError(f"chunk {p_num} missing ETag")
            return {"PartNumber": p_num, "ETag": etag}
        except Exception as e:
            log(f"Chunk {p_num} attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(min(2 * attempt, 10))
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
    return None


def process_file(path, rem_path="", user_hash="", public_share_token="", verbose=False):
    name, size = os.path.basename(path), os.path.getsize(path)
    effective_public_share = "" if (rem_path and user_hash) else public_share_token
    if verbose:
        log(
            f"START {path} ({size} bytes) rem={rem_path} "
            f"public_share={'off' if not effective_public_share else 'on'}"
        )

    if rem_path and FORCE_LEGACY_FOR_FOLDERS:
        res = legacy_upload(path, rem_path, user_hash, effective_public_share)
        return finalize_uploaded_file(res, rem_path, user_hash)

    init = api("get-upload-url", {"size": size})
    u_id, key, p_size, urls = (
        init.get("uploadId"),
        init.get("key"),
        init.get("partSize"),
        init.get("urls", []),
    )

    if not u_id or not urls or not p_size:
        res = legacy_upload(path, rem_path, user_hash, effective_public_share)
        return finalize_uploaded_file(res, rem_path, user_hash)

    results = []
    with ThreadPoolExecutor(max_workers=CHUNK_THREADS) as exe:
        tasks = [
            exe.submit(
                upload_chunk,
                urls[i],
                path,
                i * p_size,
                min(p_size, size - (i * p_size)),
                i + 1,
            )
            for i in range(len(urls))
        ]

        for task in concurrent.futures.as_completed(tasks):
            part = task.result()
            if part:
                results.append(part)

    if len(results) < len(urls):
        log(f"FAIL {name}: uploaded {len(results)}/{len(urls)} parts")
        return None

    results.sort(key=lambda x: x["PartNumber"])
    fin_data = {
        "key": key,
        "uploadId": u_id,
        "name": name,
        "user": user_hash,
    }
    if rem_path:
        fin_data["path"] = rem_path
    if effective_public_share:
        fin_data["pathPublicShare"] = effective_public_share

    for i, p in enumerate(results):
        fin_data[f"parts[{i}][PartNumber]"] = p["PartNumber"]
        fin_data[f"parts[{i}][ETag]"] = p["ETag"]

    res = output_res(api("complete-upload", fin_data), size, name)
    return finalize_uploaded_file(res, rem_path, user_hash)


def legacy_upload(path, rem_path, user_hash, public_share_token):
    name, size = os.path.basename(path), os.path.getsize(path)
    boundary = f"----Viking{int(time.time())}"

    fields = {"user": user_hash}
    if rem_path:
        fields["path"] = rem_path
    if public_share_token:
        fields["pathPublicShare"] = public_share_token

    pre = []
    for key, value in fields.items():
        pre.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
        )
    pre.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    )
    header = "".join(pre).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode()

    u_url = api("get-server").get("server", "https://upload.vikingfile.com")
    parsed = urllib.parse.urlparse(u_url)
    request_target = parsed.path or "/"
    if parsed.query:
        request_target += f"?{parsed.query}"

    conn = (
        http.client.HTTPSConnection(parsed.netloc, timeout=180)
        if parsed.scheme == "https"
        else http.client.HTTPConnection(parsed.netloc, timeout=180)
    )
    conn.putrequest("POST", request_target)
    conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
    conn.putheader("Content-Length", str(len(header) + size + len(tail)))
    conn.putheader("User-Agent", UA)
    conn.endheaders()

    conn.send(header)
    with open(path, "rb") as f:
        while True:
            payload = f.read(CHUNK_READ_SIZE)
            if not payload:
                break
            conn.send(payload)
            with progress_lock:
                global total_bytes_sent
                total_bytes_sent += len(payload)
    conn.send(tail)

    response = conn.getresponse().read().decode().strip()
    conn.close()
    data = json.loads(response) if response else {}
    return output_res(data, size, name)


def move_uploaded_file(file_hash, path, user_hash):
    if not file_hash or not path or not user_hash:
        return True
    res = api("move-file", {"hash": file_hash, "user": user_hash, "path": path})
    ok = isinstance(res, dict) and res.get("error") == "success"
    if not ok:
        log(f"move-file failed hash={file_hash} path={path}: {res}")
    return ok


def finalize_uploaded_file(res, rem_path, user_hash):
    if not isinstance(res, dict):
        return False
    file_hash = res.get("hash")
    if rem_path and user_hash and not move_uploaded_file(file_hash, rem_path, user_hash):
        return False
    return True


def output_res(res, size, display_name):
    url = res.get("url") if isinstance(res, dict) else None
    if url:
        sys.stdout.write("\r" + " " * 140 + "\r")
        print(
            f"{res.get('name', display_name)} [{human_size(size)}]\n{url}\n{base64.b64encode(url.encode()).decode()}\n{'-' * 48}"
        )
        return res

    log(f"Upload failed for {display_name}: {res}")
    sys.stdout.write("\r" + " " * 140 + "\r")
    print(f"FAILED: {display_name} [{human_size(size)}]")
    return None


def find_folder_url(folder_name, user_hash):
    """Deep scan account to find the folder's public hash."""
    if not user_hash:
        return

    data = api("list-files", {"user": user_hash, "page": 1})
    folder_hash = None

    items = data.get("files", []) if isinstance(data, dict) else []
    for item in items:
        if item.get("name") == folder_name:
            folder_hash = item.get("hash")
            break

    if folder_hash:
        f_url = f"https://vikingfile.com/folder/{folder_hash}"
        print(
            f"FOLDER: {folder_name}\n{f_url}\n{base64.b64encode(f_url.encode()).decode()}\n{'=' * 48}"
        )


def should_skip(path):
    base = os.path.basename(path)
    if base == LOG_FILE:
        return True
    if ".viking_session" in base:
        return True
    return False


def iter_upload_entries(target_path):
    if os.path.isfile(target_path):
        yield target_path, ""
        return

    folder_name = os.path.basename(os.path.normpath(target_path))
    for root, _, files in os.walk(target_path):
        for filename in files:
            full_path = os.path.join(root, filename)
            if should_skip(full_path):
                continue
            rel = os.path.relpath(root, target_path)
            # Always preserve "<root-folder>/<subfolder...>" structure on remote.
            remote_path = folder_name if rel == "." else f"{folder_name}/{rel.replace(os.sep, '/')}"
            yield full_path, remote_path


def compute_total_size(target_path):
    total = 0
    for full_path, _ in iter_upload_entries(target_path):
        try:
            total += os.path.getsize(full_path)
        except FileNotFoundError:
            continue
    return total


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("target", help="File or Folder")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--user", default=None, help="Your Viking user hash")
    parser.add_argument(
        "--public-upload-url",
        default=DEFAULT_PUBLIC_UPLOAD_URL,
        help="Public upload URL or token from /public-upload/<token>",
    )
    parser.add_argument("--file-threads", type=int, default=FILE_THREADS, help="Number of files to upload concurrently")
    parser.add_argument("--chunk-threads", type=int, default=CHUNK_THREADS, help="Number of concurrent chunks per large file")
    parser.add_argument(
        "--chunked-folders",
        action="store_true",
        help="Use chunked API for files inside folders (default uses legacy for path reliability).",
    )
    parser.add_argument("--key-file", default=KEY_FILE, help="Path to API key file")
    parser.add_argument("--log-file", default=LOG_FILE, help="Path to log file")
    parser.add_argument(
        "--resume-dir", default=RESUME_DIR, help="Directory for folder-resume state files"
    )
    args = parser.parse_args()

    LOG_FILE = args.log_file
    KEY_FILE = args.key_file
    RESUME_DIR = args.resume_dir or tempfile.gettempdir()

    if args.user is None:
        key_path = os.path.expanduser(KEY_FILE)
        if os.path.exists(key_path) and os.path.getsize(key_path) > 0:
            with open(key_path) as f:
                args.user = f.read().strip()
            if not args.user:
                args.user = DEFAULT_USER_HASH
        else:
            args.user = DEFAULT_USER_HASH
            with open(key_path, "w") as f:
                f.write(DEFAULT_USER_HASH)

    start_time = time.time()

    target_path = args.target
    if not os.path.exists(target_path):
        sys.exit("Usage: viking.py <target>")

    if args.file_threads < 1 or args.chunk_threads < 1:
        sys.exit("--file-threads and --chunk-threads must be >= 1")

    FILE_THREADS = args.file_threads
    CHUNK_THREADS = args.chunk_threads
    FORCE_LEGACY_FOR_FOLDERS = not args.chunked_folders

    public_share_token = extract_public_share_token(args.public_upload_url)
    if not public_share_token:
        sys.exit("Invalid --public-upload-url. Expected /public-upload/<token> or a raw token.")

    folder_name = (
        None if os.path.isfile(target_path) else os.path.basename(os.path.normpath(target_path))
    )
    state_path = _resume_state_path(target_path, folder_name or "file")
    resume_state = _load_resume_state(state_path)

    to_upload = []
    skipped_count = 0
    skipped_bytes = 0
    for file_path, remote_path in iter_upload_entries(target_path):
        rel_file = os.path.relpath(file_path, target_path).replace("\\", "/")
        st = os.stat(file_path)
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
        to_upload.append((file_path, remote_path, rel_file, size, mtime_ns))

    if not to_upload:
        print("No pending files found in the folder (resume checkpoint says all done).")
        sys.exit(0)

    total_size = sum(f[3] for f in to_upload)
    threading.Thread(target=draw_progress, args=(total_size,), daemon=True).start()

    ok_count = 0
    fail_count = 0

    with ThreadPoolExecutor(max_workers=FILE_THREADS) as exe:
        inflight = {}
        for file_path, remote_path, rel_file, size, mtime_ns in to_upload:
            if len(inflight) >= FILE_THREADS * 3:
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
                process_file,
                file_path,
                remote_path,
                args.user,
                public_share_token,
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
    if folder_name:
        find_folder_url(folder_name, args.user)
    print(
        f"Done. Success: {ok_count}, Failed: {fail_count}, Skipped: {skipped_count} "
        f"({human_size(skipped_bytes)})"
    )
