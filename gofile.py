#!/usr/bin/env python3
import os
import sys
import json
import time
import base64
import socket
import threading
import argparse
import http.client
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

# --- OPTIMIZED CONFIG (aria2-style) ---

FILE_THREADS = 16        # Equivalent to -x16
CHUNK_SIZE = 1024 * 1024 # 1MB buffer - equivalent to -k1M
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# --- STATE ---
progress_lock = threading.Lock()
total_bytes_sent = 0
total_size = 0
start_time = time.time()

def human_size(n):
    for u in ['B','KB','MB','GB']:
        if n < 1024: return f"{n:.2f} {u}"
        n /= 1024

def api_call(url, method="GET", data=None):
    headers = {"Authorization": f"Bearer {TOKEN}", "User-Agent": UA}
    if data:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.loads(res.read().decode())
    except: return {}

def get_best_server():
    # Attempt to get the fastest regional server
    res = api_call("https://api.gofile.io/servers")
    servers = res.get("data", {}).get("servers", [])
    return servers[0]["name"] if servers else "store1"

def get_root():
    res = api_call("https://api.gofile.io/accounts/getid")
    acc_id = res.get("data", {}).get("id")
    if acc_id:
        acc_info = api_call(f"https://api.gofile.io/accounts/{acc_id}")
        return acc_info.get("data", {}).get("rootFolder")
    return None

def create_folder(name, parent_id):
    res = api_call("https://api.gofile.io/contents/createFolder", "POST", 
                   {"parentFolderId": parent_id, "folderName": name})
    return res.get("data", {}).get("id")

def draw_progress():
    while total_bytes_sent < total_size:
        with progress_lock:
            curr = total_bytes_sent
        
        perc = (curr / total_size) * 100 if total_size > 0 else 0
        elap = time.time() - start_time
        speed = curr / elap if elap > 1 else 0
        eta = (total_size - curr) / speed if speed > 100 else 0
        
        bar_w = 30
        filled = int(bar_w * curr / total_size) if total_size > 0 else 0
        bar = "#" * filled + "-" * (bar_w - filled)
        
        # Format for Colab/Terminal compatibility
        sys.stdout.write(f"\r[{bar}] {perc:3.0f}% | {human_size(speed)}/s | ETA: {int(eta)}s ")
        sys.stdout.flush()
        time.sleep(0.2)

def upload_worker(path, folder_id, server):
    global total_bytes_sent
    name = os.path.basename(path)
    size = os.path.getsize(path)
    
    boundary = f"----GofileBoundary{int(time.time())}"
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"folderId\"\r\n\r\n{folder_id}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    
    try:
        # Create socket and tune for performance
        conn = http.client.HTTPSConnection(f"{server}.gofile.io")
        conn.connect()
        conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1) # Disable Nagle
        
        conn.putrequest("POST", "/uploadfile")
        conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length", str(len(head) + size + len(tail)))
        conn.putheader("Authorization", f"Bearer {TOKEN}")
        conn.putheader("User-Agent", UA)
        conn.endheaders()
        
        conn.send(head)
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk: break
                conn.send(chunk)
                with progress_lock:
                    total_bytes_sent += len(chunk)
        conn.send(tail)
        
        resp_data = conn.getresponse().read().decode()
        res = json.loads(resp_data)
        conn.close()
        
        link = res.get("data", {}).get("downloadPage")
        if link:
            # Clear current line for result output
            sys.stdout.write("\r" + " " * 80 + "\r")
            b64 = base64.b64encode(link.encode()).decode()
            print(f"{name} [{human_size(size)}]\n{link}\n{b64}\n{'-'*48}")
    except: pass

def main():
    global total_size
    parser = argparse.ArgumentParser()
    parser.add_argument("target")
    args = parser.parse_args()

    if not os.path.exists(args.target): return
    
    root_id = get_root()
    server = get_best_server()
    if not root_id: return

    to_upload = []
    if os.path.isfile(args.target):
        to_upload.append((args.target, root_id))
        total_size = os.path.getsize(args.target)
    else:
        base_dir = os.path.abspath(args.target)
        main_folder_id = create_folder(os.path.basename(base_dir), root_id)
        folder_map = {base_dir: main_folder_id}
        
        for root, dirs, files in os.walk(base_dir):
            curr_id = folder_map[os.path.abspath(root)]
            for d in dirs:
                l_path = os.path.abspath(os.path.join(root, d))
                folder_map[l_path] = create_folder(d, curr_id)
            for f in files:
                f_path = os.path.join(root, f)
                to_upload.append((f_path, curr_id))
                total_size += os.path.getsize(f_path)

    # Start progress tracker
    threading.Thread(target=draw_progress, daemon=True).start()

    # Parallel Upload Execution
    with ThreadPoolExecutor(max_workers=FILE_THREADS) as exe:
        list(exe.map(lambda p: upload_worker(*p, server), to_upload))

    sys.stdout.write("\r" + " " * 80 + "\r")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)