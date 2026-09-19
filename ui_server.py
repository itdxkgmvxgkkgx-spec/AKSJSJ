#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VLESS Generator - Web UI server (stdlib only, no extra deps)

Routes
  GET  /                      UI (static/index.html)
  GET  /api/status            live status.json (progress, logs)
  GET  /api/results           result.json
  GET  /api/start?total=&threads=&mode=&rounds=&timeout=   start generator (409 if running)
  POST /api/start             same, JSON body
  GET  /api/stop              kill running generator
  GET  /sub                   plain text subscription (result.txt)
  GET  /sub/b64               base64 subscription (result_b64.txt)
  GET  /download/<file>       result.txt | result_b64.txt | result.json | all_generated.txt
  GET  /events                SSE stream of status.json

Env: UI_PORT (8000), OUT_DIR (.), PYTHON (sys.executable)
"""
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.abspath(os.environ.get("OUT_DIR", ROOT))
STATIC = os.path.join(ROOT, "static")
PORT = int(os.environ.get("UI_PORT", "8000"))
PY = os.environ.get("PYTHON", sys.executable)
GEN = os.path.join(ROOT, "generator.py")
DOWNLOADABLE = {"result.txt", "result_b64.txt", "result.json", "all_generated.txt", "status.json"}

_proc_lock = threading.Lock()
_proc = None
_proc_log = os.path.join(OUT_DIR, "generator.log")


def gen_running():
    return _proc is not None and _proc.poll() is None


def start_generator(opts):
    global _proc
    with _proc_lock:
        if gen_running():
            return False, "already running"
        env = os.environ.copy()
        env["OUT_DIR"] = OUT_DIR
        env["PYTHONUNBUFFERED"] = "1"
        args = [PY, GEN]
        allowed = {"total": "--total", "threads": "--threads", "mode": "--mode",
                   "rounds": "--rounds", "timeout": "--timeout", "url": "--url"}
        for k, flag in allowed.items():
            v = opts.get(k)
            if v not in (None, "", []):
                args += [flag, str(v)]
        os.makedirs(OUT_DIR, exist_ok=True)
        logf = open(_proc_log, "w")
        _proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=logf, stderr=subprocess.STDOUT)
        return True, " ".join(args[2:])


def stop_generator():
    global _proc
    with _proc_lock:
        if gen_running():
            _proc.terminate()
            try:
                _proc.wait(3)
            except subprocess.TimeoutExpired:
                _proc.kill()
            return True
        return False


def read_json(name, default):
    p = os.path.join(OUT_DIR, name)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "VLESS-UI/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter
        if "/api/status" in (args[0] if args else "") or "/events" in (args[0] if args else ""):
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers -----------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, download=False):
        if not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if path.endswith((".txt", ".json", ".html", ".js", ".css")):
            ctype += "; charset=utf-8"
        with open(path, "rb") as f:
            data = f.read()
        extra = {"Content-Disposition": f'attachment; filename="{os.path.basename(path)}"'} if download else None
        self._send(200, data, ctype, extra)

    def _status_payload(self):
        st = read_json("status.json", {"phase": "idle", "logs": []})
        st["running"] = gen_running()
        st["server_time"] = time.time()
        for f in ("result.txt", "result.json"):
            p = os.path.join(OUT_DIR, f)
            st.setdefault("files", {})[f] = os.path.getmtime(p) if os.path.exists(p) else None
        return st

    # -- routing -----------------------------------------------------------
    def do_OPTIONS(self):
        self._send(204, b"", "text/plain", {"Access-Control-Allow-Methods": "GET,POST,OPTIONS",
                                            "Access-Control-Allow-Headers": "*"})

    def do_POST(self):
        u = urlsplit(self.path)
        if u.path == "/api/start":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                opts = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                opts = {}
            ok, msg = start_generator(opts)
            return self._send(200 if ok else 409, {"ok": ok, "message": msg})
        self._send(404, {"error": "not found"})

    def do_GET(self):
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path

        if p in ("/", "/index.html"):
            return self._file(os.path.join(STATIC, "index.html"))
        if p == "/favicon.ico" or p == "/favicon.svg":
            svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" '
                   'y1="0" x2="1" y2="1"><stop offset="0" stop-color="#7c5cff"/><stop offset="1" stop-color="#22d3ee"/>'
                   '</linearGradient></defs><rect width="64" height="64" rx="16" fill="url(#g)"/>'
                   '<path d="M36 8 18 36h12l-4 20 20-30H34z" fill="#fff"/></svg>')
            return self._send(200, svg, "image/svg+xml", {"Cache-Control": "public, max-age=86400"})
        if p.startswith("/static/"):
            fp = os.path.abspath(os.path.join(STATIC, p[len("/static/"):]))
            if not fp.startswith(STATIC):
                return self._send(403, {"error": "forbidden"})
            return self._file(fp)

        if p == "/api/status":
            return self._send(200, self._status_payload())
        if p == "/api/results":
            return self._send(200, read_json("result.json", {"meta": {}, "summary": {}, "items": []}))
        if p == "/api/start":
            ok, msg = start_generator(q)
            return self._send(200 if ok else 409, {"ok": ok, "message": msg})
        if p == "/api/stop":
            return self._send(200, {"ok": stop_generator()})
        if p == "/api/log":
            return self._file(_proc_log)
        if p == "/health":
            return self._send(200, {"ok": True, "running": gen_running()})

        if p == "/sub":
            return self._file(os.path.join(OUT_DIR, "result.txt"))
        if p in ("/sub/b64", "/sub64"):
            return self._file(os.path.join(OUT_DIR, "result_b64.txt"))
        if p.startswith("/download/"):
            name = os.path.basename(p[len("/download/"):])
            if name not in DOWNLOADABLE:
                return self._send(403, {"error": "forbidden"})
            return self._file(os.path.join(OUT_DIR, name), download=True)

        if p == "/events":
            return self._sse()

        self._send(404, {"error": "not found"})

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        last = None
        try:
            for _ in range(3600):  # ~30 min max per connection
                payload = json.dumps(self._status_payload(), ensure_ascii=False)
                if payload != last:
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    last = payload
                else:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"VLESS UI  →  http://0.0.0.0:{PORT}   (OUT_DIR={OUT_DIR})", flush=True)
    if os.environ.get("AUTOSTART", "0") == "1":
        start_generator({})
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        stop_generator()


if __name__ == "__main__":
    main()
