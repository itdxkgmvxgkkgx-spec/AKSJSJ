#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VLESS Config Generator & Tester  (v2 - high speed)
=================================================
1. Fetch subscription (BASE_URL)  -> decode base64 / plain
2. Parse every vless:// link
3. Resolve hosts (system DNS + Cloudflare/Google DoH) to find extra IPs
4. Generate TOTAL_GENERATE variants (fingerprint / address / spiderX)
5. Test all variants asynchronously (TCP -> TLS handshake -> optional real Xray test)
6. Write:
     result.txt        working links sorted by latency
     result_b64.txt    same, base64 (importable as a subscription)
     result.json       full details for the UI
     all_generated.txt every generated link (tested or not)
     status.json       live progress (polled by ui_server.py)

Env vars / CLI flags:
  BASE_URL, OUTPUT_FILE, THREADS, TOTAL_GENERATE, TEST_MODE(auto|tls|tcp|xray),
  TIMEOUT, ROUNDS, XRAY_BIN, OUT_DIR
"""
import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
DEFAULTS = {
    "BASE_URL": "https://edge.cfgrelay.top/sub/97147ed4-4eb4-4972-be7f-5fed1310881f",
    "OUTPUT_FILE": "result.txt",
    "THREADS": 8,
    "TOTAL_GENERATE": 50,
    "TEST_MODE": "auto",   # auto -> xray if binary exists, else tls
    "TIMEOUT": 6.0,
    "ROUNDS": 2,           # test each config N times, keep best latency
    "XRAY_BIN": "xray",
    "OUT_DIR": ".",
}

FINGERPRINTS = ["chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq", "random", "randomized"]
SPIDER_X = ["", "/"]
UA = "v2rayNG/1.8.29 (VLESS-Generator)"

LOG = deque(maxlen=400)
STATUS = {
    "phase": "idle",
    "message": "",
    "started_at": None,
    "finished_at": None,
    "total": 0,
    "done": 0,
    "working": 0,
    "failed": 0,
    "best_ms": None,
    "eta_sec": None,
    "source_count": 0,
    "userinfo": {},
    "profile_title": "",
    "config": {},
    "logs": [],
    "error": None,
}
_status_path = "status.json"
_last_flush = 0.0


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg, level="info"):
    line = {"t": datetime.now().strftime("%H:%M:%S"), "lvl": level, "msg": str(msg)}
    LOG.append(line)
    print(f"[{line['t']}] [{level.upper():5}] {msg}", flush=True)
    flush_status(force=(level in ("error", "ok")))


def flush_status(force=False):
    global _last_flush
    t = time.time()
    if not force and t - _last_flush < 0.25:
        return
    _last_flush = t
    STATUS["logs"] = list(LOG)[-150:]
    tmp = _status_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATUS, f, ensure_ascii=False)
        os.replace(tmp, _status_path)
    except Exception as e:  # pragma: no cover
        print("status write failed:", e, file=sys.stderr)


def set_phase(phase, message=""):
    STATUS["phase"] = phase
    STATUS["message"] = message
    flush_status(force=True)


# ----------------------------------------------------------------------------
# Fetch & parse
# ----------------------------------------------------------------------------
def _hedged_get(url, headers, connect_timeout=4.0, read_timeout=12.0, lanes=4, stagger=1.0):
    """The upstream host sometimes stalls on TCP connect for 20-130s.
    Fire staggered parallel requests and return the first successful one."""
    from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
    errors = []

    def one(i):
        time.sleep(i * stagger)
        s = requests.Session()
        s.trust_env = False
        r = s.get(url, headers=headers, timeout=(connect_timeout, read_timeout), allow_redirects=True)
        if r.status_code == 200 and r.text.strip():
            return r.text, dict(r.headers)
        raise RuntimeError(f"HTTP {r.status_code}")

    with ThreadPoolExecutor(max_workers=lanes) as ex:
        futs = {ex.submit(one, i) for i in range(lanes)}
        while futs:
            done, futs = wait(futs, return_when=FIRST_COMPLETED)
            for f in done:
                try:
                    res = f.result()
                    for p in futs:
                        p.cancel()
                    return res
                except Exception as e:
                    errors.append(str(e)[:100])
    raise RuntimeError("; ".join(errors) or "all lanes failed")


def fetch_subscription(url, timeout=20):
    """Return (text, headers). Hedged requests -> cloudscraper -> curl -> cached copy."""
    headers = {"User-Agent": UA, "Accept": "*/*"}
    errors = []
    if requests:
        for attempt in range(2):
            try:
                text, hdrs = _hedged_get(url, headers)
                try:
                    with open(os.path.join(os.path.dirname(_status_path) or ".", "source_cache.txt"), "w") as f:
                        f.write(text)
                except Exception:
                    pass
                return text, hdrs
            except Exception as e:
                errors.append(str(e))
    try:
        import cloudscraper  # type: ignore
        s = cloudscraper.create_scraper()
        r = s.get(url, headers=headers, timeout=timeout)
        if r.status_code == 200 and r.text.strip():
            return r.text, dict(r.headers)
        errors.append(f"cloudscraper HTTP {r.status_code}")
    except Exception as e:
        errors.append(f"cloudscraper: {e}")
    try:
        out = subprocess.run(["curl", "-sL", "-m", str(timeout), "-A", UA, url],
                             capture_output=True, text=True, timeout=timeout + 5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout, {}
        errors.append(f"curl rc={out.returncode}")
    except Exception as e:
        errors.append(f"curl: {e}")
    cache = os.path.join(os.path.dirname(_status_path) or ".", "source_cache.txt")
    if os.path.exists(cache):
        log("Upstream unreachable, using cached subscription copy", "warn")
        with open(cache, encoding="utf-8") as f:
            return f.read(), {}
    raise RuntimeError("Fetch failed: " + " | ".join(errors))


def maybe_b64(text):
    t = text.strip()
    if "://" in t[:20]:
        return t
    t2 = re.sub(r"\s+", "", t)
    t2 += "=" * (-len(t2) % 4)
    for dec in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            d = dec(t2).decode("utf-8", "ignore")
            if "://" in d:
                return d
        except Exception:
            pass
    return t


def parse_userinfo(headers):
    """subscription-userinfo: upload=0; download=1; total=2; expire=3"""
    h = {k.lower(): v for k, v in headers.items()}
    info = {}
    raw = h.get("subscription-userinfo", "")
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            try:
                info[k] = int(v)
            except ValueError:
                info[k] = v
    title = h.get("profile-title", "")
    if title.startswith("base64:"):
        try:
            title = base64.b64decode(title[7:] + "==").decode("utf-8", "ignore")
        except Exception:
            pass
    info["update_interval_h"] = h.get("profile-update-interval")
    return info, title


def parse_vless(link):
    if not link.lower().startswith("vless://"):
        return None
    try:
        u = urlsplit(link.strip())
        uuid = unquote(u.username or "")
        host = u.hostname or ""
        port = u.port or 443
        q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        name = unquote(u.fragment or "")
        if not uuid or not host:
            return None
        return {"uuid": uuid, "host": host, "port": port, "params": q, "name": name, "raw": link.strip()}
    except Exception:
        return None


def build_vless(cfg, address=None, params=None, name=None):
    addr = address or cfg["host"]
    if ":" in addr and not addr.startswith("["):
        addr = f"[{addr}]"
    p = dict(cfg["params"])
    if params:
        p.update(params)
    p = {k: v for k, v in p.items() if v is not None}
    query = urlencode(p, safe="/:@,-_.~")
    frag = quote(name if name is not None else cfg["name"], safe="")
    return f"vless://{cfg['uuid']}@{addr}:{cfg['port']}?{query}#{frag}"


# ----------------------------------------------------------------------------
# DNS
# ----------------------------------------------------------------------------
def resolve_system(host):
    ips = set()
    try:
        for fam, _, _, _, sa in socket.getaddrinfo(host, None):
            if fam == socket.AF_INET:
                ips.add(sa[0])
    except Exception:
        pass
    return ips


def resolve_doh(host, timeout=3):
    ips = set()
    if not requests:
        return ips
    endpoints = [
        ("https://cloudflare-dns.com/dns-query", {"name": host, "type": "A"}, {"accept": "application/dns-json"}),
        ("https://dns.google/resolve", {"name": host, "type": "A"}, {}),
    ]
    for url, params, hdr in endpoints:
        try:
            r = requests.get(url, params=params, headers=hdr, timeout=timeout)
            for a in r.json().get("Answer", []) or []:
                if a.get("type") == 1:
                    ips.add(a["data"])
        except Exception:
            pass
    return ips


def is_ip(s):
    try:
        socket.inet_aton(s)
        return True
    except OSError:
        return ":" in s


# ----------------------------------------------------------------------------
# Generation
# ----------------------------------------------------------------------------
def generate_variants(bases, total):
    """Round-robin over base configs so the output stays diverse."""
    log(f"Resolving {len(bases)} hosts (parallel) ...")
    from concurrent.futures import ThreadPoolExecutor

    def _resolve(b):
        if is_ip(b["host"]):
            return b["host"], set()
        return b["host"], resolve_system(b["host"]) | resolve_doh(b["host"])

    with ThreadPoolExecutor(max_workers=min(32, max(4, len(bases) * 2))) as ex:
        resolved = dict(ex.map(_resolve, bases))

    per_base = []
    for b in bases:
        addrs = [b["host"]]
        ips = resolved.get(b["host"], set())
        if not is_ip(b["host"]):
            addrs += sorted(ips)
            log(f"  {b['host']} -> {', '.join(sorted(ips)) or 'no A record'}")
        variants = []
        for fp in FINGERPRINTS:
            for addr in addrs:
                for spx in SPIDER_X:
                    params = {"fp": fp}
                    if spx and b["params"].get("security") == "reality":
                        params["spx"] = spx
                    elif "spx" in b["params"] and not spx:
                        params["spx"] = b["params"]["spx"]
                    tag = fp if addr == b["host"] else f"{fp}·{addr}"
                    if spx:
                        tag += "·spx"
                    variants.append({
                        "base": b, "address": addr, "fp": fp, "spx": spx,
                        "tag": tag,
                        "link": build_vless(b, address=addr, params=params, name=f"{b['name']} | {tag}"),
                    })
        per_base.append(variants)

    out, seen = [], set()
    idx = 0
    while len(out) < total and any(idx < len(v) for v in per_base):
        for v in per_base:
            if idx < len(v) and len(out) < total:
                item = v[idx]
                if item["link"] not in seen:
                    seen.add(item["link"])
                    out.append(item)
        idx += 1
    for i, v in enumerate(out, 1):
        v["id"] = i
    return out


# ----------------------------------------------------------------------------
# Testing
# ----------------------------------------------------------------------------
def make_ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(["h2", "http/1.1"])
    return ctx


async def probe_once(item, timeout, do_tls, ctx):
    """Returns dict: ok, tcp_ms, tls_ms, err"""
    base = item["base"]
    host, port = item["address"], base["port"]
    sni = base["params"].get("sni") or base["params"].get("host") or host
    res = {"ok": False, "tcp_ms": None, "tls_ms": None, "err": None, "cert_cn": None}
    t0 = time.perf_counter()
    writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        res["tcp_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        if do_tls:
            t1 = time.perf_counter()
            await asyncio.wait_for(writer.start_tls(ctx, server_hostname=sni), timeout)
            res["tls_ms"] = round((time.perf_counter() - t1) * 1000, 1)
            sslobj = writer.get_extra_info("ssl_object")
            if sslobj:
                res["cert_cn"] = sslobj.version()
        res["ok"] = True
    except asyncio.TimeoutError:
        res["err"] = "timeout"
    except (ConnectionRefusedError, ConnectionResetError) as e:
        res["err"] = type(e).__name__
    except ssl.SSLError as e:
        res["err"] = f"ssl: {e.reason or e}"
    except OSError as e:
        res["err"] = e.strerror or str(e)
    except Exception as e:
        res["err"] = str(e)[:80]
    finally:
        if writer:
            try:
                writer.close()
            except Exception:
                pass
    return res


async def run_probe_tests(items, threads, timeout, rounds, do_tls):
    sem = asyncio.Semaphore(threads)
    ctx = make_ssl_ctx()
    started = time.time()

    async def worker(item):
        async with sem:
            best = None
            for _ in range(rounds):
                r = await probe_once(item, timeout, do_tls, ctx)
                if r["ok"]:
                    lat = r["tls_ms"] if do_tls else r["tcp_ms"]
                    if best is None or not best["ok"] or lat < (best["tls_ms"] if do_tls else best["tcp_ms"]):
                        best = r
                elif best is None:
                    best = r
            item["probe"] = best
            item["ok"] = best["ok"]
            item["latency_ms"] = (best["tls_ms"] if do_tls else best["tcp_ms"]) if best["ok"] else None
            item["error"] = best["err"]
            STATUS["done"] += 1
            if best["ok"]:
                STATUS["working"] += 1
                if STATUS["best_ms"] is None or item["latency_ms"] < STATUS["best_ms"]:
                    STATUS["best_ms"] = item["latency_ms"]
                log(f"✓ #{item['id']:<3} {item['address']:<28} {item['tag']:<18} {item['latency_ms']:>7} ms", "ok")
            else:
                STATUS["failed"] += 1
                log(f"✗ #{item['id']:<3} {item['address']:<28} {item['tag']:<18} {best['err']}", "warn")
            el = time.time() - started
            if STATUS["done"]:
                STATUS["eta_sec"] = round(el / STATUS["done"] * (STATUS["total"] - STATUS["done"]))
            flush_status()

    await asyncio.gather(*(worker(i) for i in items))


# ---- Real test through Xray core -------------------------------------------
def xray_outbound(item):
    b = item["base"]
    p = b["params"]
    net = p.get("type", "tcp")
    if net == "raw":
        net = "tcp"
    stream = {"network": net, "security": p.get("security", "none")}
    if stream["security"] == "reality":
        stream["realitySettings"] = {
            "serverName": p.get("sni", ""), "fingerprint": item["fp"],
            "publicKey": p.get("pbk", ""), "shortId": p.get("sid", ""), "spiderX": p.get("spx", item["spx"] or "/"),
        }
    elif stream["security"] == "tls":
        stream["tlsSettings"] = {"serverName": p.get("sni", ""), "fingerprint": item["fp"], "allowInsecure": True}
        if p.get("alpn"):
            stream["tlsSettings"]["alpn"] = p["alpn"].split(",")
    if net == "ws":
        stream["wsSettings"] = {"path": p.get("path", "/"), "headers": {"Host": p.get("host", "")}}
    elif net == "grpc":
        stream["grpcSettings"] = {"serviceName": p.get("serviceName", "")}
    elif net == "httpupgrade":
        stream["httpupgradeSettings"] = {"path": p.get("path", "/"), "host": p.get("host", "")}
    elif net == "xhttp":
        stream["xhttpSettings"] = {"path": p.get("path", "/"), "host": p.get("host", ""), "mode": p.get("mode", "auto")}
    user = {"id": b["uuid"], "encryption": p.get("encryption", "none")}
    if p.get("flow"):
        user["flow"] = p["flow"]
    return {
        "protocol": "vless", "tag": "proxy",
        "settings": {"vnext": [{"address": item["address"], "port": b["port"], "users": [user]}]},
        "streamSettings": stream,
    }


async def xray_test(item, port, xray_bin, timeout):
    cfg = {
        "log": {"loglevel": "none"},
        "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "socks", "settings": {"udp": False}}],
        "outbounds": [xray_outbound(item)],
    }
    fd, path = tempfile.mkstemp(suffix=".json", prefix="xr_")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            xray_bin, "run", "-c", path, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        # wait for socks port
        for _ in range(40):
            await asyncio.sleep(0.05)
            try:
                r, w = await asyncio.open_connection("127.0.0.1", port)
                w.close()
                break
            except OSError:
                continue
        curl = await asyncio.create_subprocess_exec(
            "curl", "-s", "-o", "/dev/null", "-m", str(timeout), "--socks5-hostname", f"127.0.0.1:{port}",
            "-w", "%{http_code} %{time_total}", "http://cp.cloudflare.com/",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(curl.communicate(), timeout + 3)
        code, tt = (out.decode().split() + ["0", "0"])[:2]
        if code in ("204", "200", "301", "302"):
            return True, round(float(tt) * 1000, 1), None
        return False, None, f"http {code}"
    except Exception as e:
        return False, None, str(e)[:60]
    finally:
        if proc:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), 2)
            except Exception:
                proc.kill()
        try:
            os.unlink(path)
        except OSError:
            pass


async def run_xray_tests(items, threads, timeout, xray_bin):
    sem = asyncio.Semaphore(max(1, threads))
    base_port = 21000

    async def worker(i, item):
        async with sem:
            ok, ms, err = await xray_test(item, base_port + i, xray_bin, timeout)
            item["xray_ok"] = ok
            item["xray_ms"] = ms
            if ok:
                item["latency_ms"] = ms
                if STATUS["best_ms"] is None or ms < STATUS["best_ms"]:
                    STATUS["best_ms"] = ms
                log(f"⚡ #{item['id']:<3} {item['tag']:<18} real {ms} ms", "ok")
            else:
                item["ok"] = False
                item["error"] = f"xray: {err}"
                STATUS["working"] -= 1
                STATUS["failed"] += 1
                log(f"✗ #{item['id']:<3} {item['tag']:<18} xray failed: {err}", "warn")
            STATUS["done"] += 1
            flush_status()

    await asyncio.gather(*(worker(i, it) for i, it in enumerate(items)))


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------
def flag_of(name):
    m = re.search(r"[\U0001F1E6-\U0001F1FF]{2}", name or "")
    return m.group(0) if m else ""


def write_outputs(items, out_dir, output_file, meta):
    os.makedirs(out_dir, exist_ok=True)
    working = sorted([i for i in items if i.get("ok")], key=lambda x: x["latency_ms"])
    lines = []
    for rank, it in enumerate(working, 1):
        b = it["base"]
        name = f"{flag_of(b['name'])} {rank:02d} ⚡{int(it['latency_ms'])}ms | {it['tag']} | {b['name'].strip()}".strip()
        it["final_name"] = name
        it["final_link"] = build_vless(b, address=it["address"],
                                       params={"fp": it["fp"], **({"spx": it["spx"]} if it["spx"] else {})},
                                       name=name)
        lines.append(it["final_link"])

    p_txt = os.path.join(out_dir, output_file)
    with open(p_txt, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    with open(os.path.join(out_dir, "result_b64.txt"), "w") as f:
        f.write(base64.b64encode("\n".join(lines).encode()).decode())
    with open(os.path.join(out_dir, "all_generated.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(i["link"] for i in items) + "\n")

    detail = []
    for it in items:
        b = it["base"]
        detail.append({
            "id": it["id"], "ok": bool(it.get("ok")), "latency_ms": it.get("latency_ms"),
            "tcp_ms": (it.get("probe") or {}).get("tcp_ms"), "tls_ms": (it.get("probe") or {}).get("tls_ms"),
            "xray_ms": it.get("xray_ms"), "error": it.get("error"),
            "host": b["host"], "address": it["address"], "port": b["port"], "sni": b["params"].get("sni"),
            "fp": it["fp"], "spx": it["spx"], "tag": it["tag"], "flag": flag_of(b["name"]),
            "source_name": b["name"], "name": it.get("final_name") or f"{b['name']} | {it['tag']}",
            "security": b["params"].get("security"), "network": b["params"].get("type"),
            "flow": b["params"].get("flow"), "link": it.get("final_link") or it["link"],
        })
    detail.sort(key=lambda d: (not d["ok"], d["latency_ms"] if d["latency_ms"] is not None else 1e9))
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "summary": {
            "generated": len(items), "working": len(working), "failed": len(items) - len(working),
            "best_ms": working[0]["latency_ms"] if working else None,
            "avg_ms": round(sum(w["latency_ms"] for w in working) / len(working), 1) if working else None,
        }, "items": detail}, f, ensure_ascii=False, indent=1)
    return working


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def env_or(name, cast=str):
    v = os.environ.get(name)
    if v is None or v == "":
        return cast(DEFAULTS[name]) if name in DEFAULTS else None
    return cast(v)


def main():
    global _status_path
    ap = argparse.ArgumentParser(description="VLESS generator & tester")
    ap.add_argument("--url", default=env_or("BASE_URL"))
    ap.add_argument("--output", default=env_or("OUTPUT_FILE"))
    ap.add_argument("--threads", type=int, default=env_or("THREADS", int))
    ap.add_argument("--total", type=int, default=env_or("TOTAL_GENERATE", int))
    ap.add_argument("--mode", default=env_or("TEST_MODE"), choices=["auto", "tls", "tcp", "xray"])
    ap.add_argument("--timeout", type=float, default=env_or("TIMEOUT", float))
    ap.add_argument("--rounds", type=int, default=env_or("ROUNDS", int))
    ap.add_argument("--xray-bin", default=env_or("XRAY_BIN"))
    ap.add_argument("--out-dir", default=env_or("OUT_DIR"))
    a = ap.parse_args()

    os.makedirs(a.out_dir, exist_ok=True)
    _status_path = os.path.join(a.out_dir, "status.json")
    STATUS.update({"started_at": now_iso(), "finished_at": None, "total": 0, "done": 0, "working": 0,
                   "failed": 0, "best_ms": None, "error": None,
                   "config": {"url": a.url, "threads": a.threads, "total": a.total, "mode": a.mode,
                              "timeout": a.timeout, "rounds": a.rounds}})
    t_start = time.time()
    try:
        set_phase("fetch", "دریافت لینک اشتراک ...")
        log(f"Fetching subscription: {a.url}")
        text, headers = fetch_subscription(a.url)
        userinfo, title = parse_userinfo(headers)
        STATUS["userinfo"], STATUS["profile_title"] = userinfo, title
        decoded = maybe_b64(text)
        links = [l.strip() for l in decoded.splitlines() if l.strip()]
        bases = [c for c in (parse_vless(l) for l in links) if c]
        others = len(links) - len(bases)
        STATUS["source_count"] = len(bases)
        log(f"Found {len(bases)} vless configs" + (f" ({others} non-vless skipped)" if others else ""))
        if not bases:
            raise RuntimeError("No vless:// links found in subscription")

        set_phase("generate", "تولید کانفیگ‌ها ...")
        items = generate_variants(bases, a.total)
        STATUS["total"] = len(items)
        log(f"Generated {len(items)} variants (fp × address × spiderX)")

        xray_bin = shutil.which(a.xray_bin)
        mode = a.mode
        if mode == "auto":
            mode = "xray" if xray_bin else "tls"
        if mode == "xray" and not xray_bin:
            log("xray binary not found, falling back to TLS test", "warn")
            mode = "tls"
        STATUS["config"]["effective_mode"] = mode

        set_phase("test", f"تست {'TCP' if mode == 'tcp' else 'TLS handshake'} با {a.threads} نخ ...")
        log(f"Testing {len(items)} configs | mode={mode} threads={a.threads} timeout={a.timeout}s rounds={a.rounds}")
        asyncio.run(run_probe_tests(items, a.threads, a.timeout, a.rounds, do_tls=(mode != "tcp")))
        log(f"Probe done: {STATUS['working']} working / {STATUS['failed']} failed")

        if mode == "xray":
            alive = [i for i in items if i.get("ok")]
            STATUS["total"] = len(items) + len(alive)
            STATUS["best_ms"] = None
            set_phase("xray", f"تست واقعی با Xray روی {len(alive)} کانفیگ ...")
            asyncio.run(run_xray_tests(alive, a.threads, a.timeout + 4, xray_bin))

        set_phase("write", "نوشتن خروجی ...")
        meta = {"generated_at": now_iso(), "source": a.url, "mode": mode, "threads": a.threads,
                "duration_sec": round(time.time() - t_start, 1), "userinfo": userinfo, "profile_title": title,
                "source_count": len(bases)}
        working = write_outputs(items, a.out_dir, a.output, meta)
        STATUS["finished_at"] = now_iso()
        STATUS["eta_sec"] = 0
        set_phase("done", f"{len(working)} کانفیگ سالم از {len(items)} در {meta['duration_sec']} ثانیه")
        log(f"Wrote {a.output} ({len(working)} links), result_b64.txt, result.json, all_generated.txt", "ok")
        log(f"Finished in {meta['duration_sec']}s | best {STATUS['best_ms']} ms", "ok")
        return 0
    except Exception as e:
        STATUS["error"] = str(e)
        STATUS["finished_at"] = now_iso()
        set_phase("error", str(e))
        log(f"ERROR: {e}", "error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
