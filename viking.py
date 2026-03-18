#!/usr/bin/env python3
import os, sys, json, time, base64, argparse, threading, urllib.request, urllib.parse, http.client
from concurrent.futures import ThreadPoolExecutor

# --- CONFIG ---
USER_HASH = "PASTE_USER_HASH_HERE"
API_BASE = "https://vikingfile.com/api"
FILE_THREADS = 4       # Files at once
CHUNK_THREADS = 4      # Chunks per large file
LOG_FILE = ".upload_logs"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# --- STATE ---
progress_lock = threading.Lock()
total_bytes_sent = 0
start_time = time.time()

def log(msg):
    with open(LOG_FILE, "a") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")

def human_size(n):
    for u in ['B','KB','MB','GB']:
        if n < 1024: return f"{n:.2f} {u}"
        n /= 1024

def draw_progress(total_size):
    spin = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
    idx = 0
    while total_bytes_sent < total_size:
        curr = total_bytes_sent
        perc = (curr / total_size) * 100
        elap = time.time() - start_time
        speed = curr / elap if elap > 1 else 0
        eta = (total_size - curr) / speed if speed > 100 else 0
        bar_w = 50
        filled = int(bar_w * curr / total_size)
        bar = "#" * filled + ">" + "-" * (bar_w - filled - 1)
        if curr >= total_size: bar = "#" * bar_w
        sys.stdout.write(f"\r{spin[idx%10]} {perc:3.0f}% [{bar[:bar_w]}] {human_size(curr)} {human_size(speed)}/s ETA {int(eta)}s ")
        sys.stdout.flush()
        idx += 1
        time.sleep(0.1)

def api(end, data=None):
    url = f"{API_BASE}/{end}"
    headers = {"User-Agent": UA, "Referer": "https://vikingfile.com/"}
    req_data = urllib.parse.urlencode(data).encode() if data else None
    try:
        req = urllib.request.Request(url, data=req_data, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode())
    except Exception as e:
        log(f"API Error {end}: {e}")
        return {}

def upload_chunk(url, path, offset, size, p_num):
    """Native socket streaming for perfect progress bar updates."""
    try:
        p = urllib.parse.urlparse(url)
        conn = http.client.HTTPSConnection(p.netloc) if p.scheme == 'https' else http.client.HTTPConnection(p.netloc)
        conn.putrequest("PUT", f"{p.path}?{p.query}")
        conn.putheader("Content-Length", str(size))
        conn.putheader("User-Agent", UA)
        conn.endheaders()

        with open(path, 'rb') as f:
            f.seek(offset)
            sent = 0
            while sent < size:
                chunk = f.read(min(128*1024, size - sent)) # 128KB blocks
                if not chunk: break
                conn.send(chunk)
                sent += len(chunk)
                with progress_lock:
                    global total_bytes_sent
                    total_bytes_sent += len(chunk)
        
        res = conn.getresponse()
        etag = res.getheader('ETag', '').replace('"', '')
        conn.close()
        return {"PartNumber": p_num, "ETag": etag} if etag else None
    except Exception as e: log(f"Chunk {p_num} Fail: {e}")

def process_file(path, rem_path=""):
    name, size = os.path.basename(path), os.path.getsize(path)
    init = api("get-upload-url", {"size": size})
    u_id, key, p_size, urls = init.get('uploadId'), init.get('key'), init.get('partSize'), init.get('urls', [])

    if not u_id: return legacy_upload(path, rem_path)

    with ThreadPoolExecutor(max_workers=CHUNK_THREADS) as exe:
        tasks = [exe.submit(upload_chunk, urls[i], path, i*p_size, min(p_size, size-(i*p_size)), i+1) for i in range(len(urls))]
        results = [t.result() for t in tasks if t.result()]

    if len(results) < len(urls): return
    results.sort(key=lambda x: x['PartNumber'])
    fin_data = {"key": key, "uploadId": u_id, "name": name, "user": USER_HASH, "path": rem_path}
    for i, p in enumerate(results):
        fin_data[f"parts[{i}][PartNumber]"], fin_data[f"parts[{i}][ETag]"] = p['PartNumber'], p['ETag']
    
    output_res(api("complete-upload", fin_data), size)

def legacy_upload(path, rem_path):
    name, size = os.path.basename(path), os.path.getsize(path)
    b = f'----Viking{int(time.time())}'
    h = f'--{b}\r\nContent-Disposition: form-data; name="user"\r\n\r\n{USER_HASH}\r\n--{b}\r\nContent-Disposition: form-data; name="path"\r\n\r\n{rem_path}\r\n--{b}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
    t = f'\r\n--{b}--\r\n'.encode()
    
    u_url = api("get-server").get('server', 'https://upload.vikingfile.com')
    p = urllib.parse.urlparse(u_url)
    conn = http.client.HTTPSConnection(p.netloc) if p.scheme == 'https' else http.client.HTTPConnection(p.netloc)
    conn.putrequest("POST", p.path or "/")
    conn.putheader("Content-Type", f"multipart/form-data; boundary={b}")
    conn.putheader("Content-Length", str(len(h) + size + len(t)))
    conn.putheader("User-Agent", UA)
    conn.endheaders()
    
    conn.send(h)
    with open(path,'rb') as f:
        while True:
            chunk = f.read(128*1024)
            if not chunk: break
            conn.send(chunk)
            with progress_lock:
                global total_bytes_sent
                total_bytes_sent += len(chunk)
    conn.send(t)
    output_res(json.loads(conn.getresponse().read().decode()), size)

def output_res(res, size):
    url = res.get('url')
    if url:
        sys.stdout.write("\r" + " " * 120 + "\r")
        print(f"{res.get('name')} [{human_size(size)}]\n{url}\n{base64.b64encode(url.encode()).decode()}\n{'-'*48}")

def find_folder_url(folder_name):
    """Deep scan account to find the folder's public hash."""
    # List files in root to find the folder entry
    data = api("list-files", {"user": USER_HASH, "page": 1})
    folder_hash = None
    
    # Viking/YetiShare usually returns folders in the same list or via search
    items = data.get('files', [])
    for item in items:
        # We look for an item with the folder name that doesn't have an extension 
        # or has a specific folder flag (if available)
        if item.get('name') == folder_name:
            folder_hash = item.get('hash')
            break
            
    if folder_hash:
        f_url = f"https://vikingfile.com/folder/{folder_hash}"
        print(f"FOLDER: {folder_name}\n{f_url}\n{base64.b64encode(f_url.encode()).decode()}\n{'='*48}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("target", nargs='*', help="File or Folder")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(); start_time = time.time()
    
    target_path = " ".join(args.target) if args.target else None
    if not target_path or not os.path.exists(target_path): sys.exit("Usage: viking.py <target>")
    
    to_upload, folder_name = [], None
    if os.path.isfile(target_path):
        to_upload.append((target_path, ""))
    else:
        folder_name = os.path.basename(os.path.normpath(target_path))
        for r, _, fs in os.walk(target_path):
            for f in fs:
                if f in [sys.argv[0], LOG_FILE] or ".viking_session" in f: continue
                rel = os.path.relpath(r, target_path)
                to_upload.append((os.path.join(r, f), folder_name if rel == "." else os.path.join(folder_name, rel)))

    total_size = sum(os.path.getsize(f[0]) for f in to_upload)
    threading.Thread(target=draw_progress, args=(total_size,), daemon=True).start()

    with ThreadPoolExecutor(max_workers=FILE_THREADS) as exe:
        [exe.submit(process_file, f[0], f[1]) for f in to_upload]
    
    sys.stdout.write("\r" + " " * 120 + "\r")
    if folder_name:
        find_folder_url(folder_name)
    print("Done.")