#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
4eleven (411) — the information line.
A zero-dependency server information dashboard.

Serves a live-updating, themeable web dashboard that shows system specs,
hardware, OS, network, DNS and more. Uses ONLY the Python 3 standard library
so it installs on any Linux box without pip, apt packages or node.

Endpoints:
    /            dashboard (dashboard.html, served from this file's directory)
    /api/info    full system snapshot as JSON (poll this every 1-2s)
    /healthz     liveness probe
    /favicon.ico inline SVG favicon

Run:
    python3 server.py [--port 4110] [--host 0.0.0.0] [--password SECRET]
Env overrides: 4ELEVEN_PORT, 4ELEVEN_HOST, 4ELEVEN_PASSWORD, 4ELEVEN_NO_PUBLIC
"""

import argparse
import glob
import hmac
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NAME = "4eleven"
VERSION = "1.0.2"
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") or 4096
HZ = os.sysconf("SC_CLK_TCK") or 100

FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#0b0f1a"/>'
    '<text x="32" y="45" font-family="monospace" font-size="38" font-weight="bold" '
    'fill="#22d3ee" text-anchor="middle">4</text></svg>'
)

# ---------------------------------------------------------------------------
# small read helpers
# ---------------------------------------------------------------------------

def read(path, default=None):
    """Read a file, returning default on any error."""
    try:
        with open(path, "r", errors="replace") as f:
            return f.read().strip()
    except (OSError, IOError):
        return default


def read_int(path):
    try:
        return int(read(path) or 0)
    except ValueError:
        return 0


def first(iterable, default=None):
    for x in iterable:
        if x:
            return x
    return default


# ---------------------------------------------------------------------------
# static system facts
# ---------------------------------------------------------------------------

def os_pretty():
    for p in ("/etc/os-release", "/usr/lib/os-release"):
        t = read(p)
        if t:
            for line in t.splitlines():
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
            for line in t.splitlines():
                if line.startswith("NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    return sys.platform


def cpu_info():
    t = read("/proc/cpuinfo") or ""
    model = vendor = mhz = cache = None
    logical = 0
    sockets = set()
    cores = set()
    flags = None
    for block in t.split("\n\n"):
        kv = {}
        for line in block.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                kv[k.strip()] = v.strip()
        if not kv:
            continue
        logical += 1
        if "physical id" in kv:
            sockets.add(kv["physical id"])
        if "core id" in kv and "physical id" in kv:
            cores.add((kv["physical id"], kv["core id"]))
        if model is None:
            model = kv.get("model name")
        if vendor is None:
            vendor = kv.get("vendor_id")
        if mhz is None:
            mhz = kv.get("cpu MHz")
        if cache is None:
            cache = kv.get("cache size")
        if flags is None:
            flags = kv.get("flags", "")
    return {
        "model": model or "unknown",
        "vendor": vendor or "unknown",
        "logical": logical,
        "physical_cores": len(cores) or logical,
        "sockets": len(sockets) or 1,
        "base_mhz": mhz,
        "cache": cache,
        "flags_count": len(flags.split()) if flags else 0,
        "virtualized": bool(re.search(r"\bhypervisor\b", flags)),
    }


def cpu_freq():
    cur = read_int("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    mx = read_int("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq")
    return {
        "current_mhz": round(cur / 1000) if cur else None,
        "max_mhz": round(mx / 1000) if mx else None,
    }


def memory_info():
    mem = {}
    t = read("/proc/meminfo") or ""
    for line in t.splitlines():
        k, _, v = line.partition(":")
        try:
            mem[k.strip()] = int(v.strip().split()[0]) * 1024
        except (ValueError, IndexError):
            pass
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", mem.get("MemFree", 0))
    used = max(total - available, 0)
    swap_total = mem.get("SwapTotal", 0)
    swap_used = max(swap_total - mem.get("SwapFree", 0), 0)
    return {
        "total": total,
        "used": used,
        "free": mem.get("MemFree", 0),
        "available": available,
        "cached": mem.get("Cached", 0) + mem.get("SReclaimable", 0),
        "percent": round(100 * used / total, 1) if total else 0,
        "swap_total": swap_total,
        "swap_used": swap_used,
        "swap_percent": round(100 * swap_used / swap_total, 1) if swap_total else 0,
    }


def storage():
    """Real filesystems with usage, deduped by device (root wins)."""
    t = read("/proc/mounts") or ""
    mounts = {}
    for line in t.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mnt, fstype = parts[0], parts[1], parts[2]
        if dev == "none" and fstype not in ("zfs",):
            continue
        is_real = dev.startswith("/dev/") or fstype in ("zfs", "overlay") or mnt == "/"
        if not is_real or not os.path.isdir(mnt):
            continue
        if dev in mounts and mounts[dev]["mount"] != "/":
            continue
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if total == 0:
            continue
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        avail = st.f_bavail * st.f_frsize
        mounts[dev] = {
            "device": dev,
            "mount": mnt,
            "fs": fstype,
            "total": total,
            "used": used,
            "avail": avail,
            "percent": round(100 * used / total, 1),
        }
    return sorted(mounts.values(), key=lambda m: m["mount"])


def block_devices():
    out = []
    base = "/sys/block"
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for d in names:
        if d.startswith(("loop", "ram", "fd")):
            continue
        size = read_int(f"{base}/{d}/size") * 512
        if size <= 0:
            continue
        model = (read(f"{base}/{d}/device/model") or "").strip()
        vendor = (read(f"{base}/{d}/device/vendor") or "").strip()
        rotational = read(f"{base}/{d}/queue/rotational")
        dtype = "HDD" if rotational == "1" else ("SSD" if rotational == "0" else "Virtual")
        if not model and d.startswith(("vd", "nvme", "xvd")):
            dtype = "Virtual"
        out.append({
            "name": d,
            "model": model or vendor or d,
            "size_gb": round(size / 1e9, 1),
            "type": dtype,
        })
    return out


def dmi():
    base = "/sys/class/dmi/id"
    out = {}
    for key in ("sys_vendor", "product_name", "product_version",
                "board_vendor", "board_name", "board_version",
                "bios_vendor", "bios_version"):
        v = read(f"{base}/{key}")
        if v:
            out[key] = v
    return out


def gpu_info():
    try:
        r = subprocess.run(["lspci"], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return []
    out = []
    for line in r.splitlines():
        if re.search(r"VGA compatible|3D controller|Display controller", line):
            rest = line.split(" ", 1)[1] if " " in line else line
            out.append(re.sub(r"\s*\(rev [0-9a-f]+\)\s*$", "", rest.strip()))
    return out[:4]


def temps():
    out = []
    seen = set()
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = read(os.path.join(hw, "name")) or "sensor"
        for f in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
            base = f[:-6]
            label = (read(base + "_label") or "").strip()
            crit = read(base + "_crit")
            raw = read(f)
            if raw is None:
                continue
            try:
                value = round(int(raw) / 1000, 1)
            except ValueError:
                continue
            key = label or os.path.basename(base)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "label": f"{name} {label}".strip() if label else f"{name}",
                "value": value,
                "crit": round(int(crit) / 1000, 1) if crit else None,
            })
    return out[:10]


# ---------------------------------------------------------------------------
# network facts
# ---------------------------------------------------------------------------

def _ifname_list():
    try:
        return sorted(os.listdir("/sys/class/net"))
    except OSError:
        return []


def _ipv4_addr(ifname):
    """ioctl SIOCGIFADDR — no `ip` command needed."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl_ioctl(s, ifname)
        return socket.inet_ntoa(packed[20:24])
    except OSError:
        return None
    finally:
        s.close()


def fcntl_ioctl(sock, ifname):
    import fcntl
    return fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", ifname[:15].encode()))


def _ipv6_addrs():
    """Parse /proc/net/if_inet6 → {ifname: [addr, ...]}"""
    idx_map = {}
    for d in _ifname_list():
        idx = read(f"/sys/class/net/{d}/ifindex")
        if idx:
            idx_map[idx] = d
    out = {}
    t = read("/proc/net/if_inet6") or ""
    for line in t.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        addr_hex, idx, scope = parts[0], parts[1], parts[3]
        name = idx_map.get(idx)
        if not name:
            continue
        try:
            addr = ipaddress.IPv6Address(int(addr_hex, 16)).compressed
        except ValueError:
            continue
        scope_name = {0x00: "global", 0x10: "host", 0x20: "link", 0x40: "site"}.get(
            int(scope, 16), "other")
        out.setdefault(name, []).append({"addr": addr, "family": 6, "scope": scope_name})
    return out


def gateways():
    out = []
    t = read("/proc/net/route") or ""
    for line in t.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "00000000" and parts[2] != "00000000":
            gw = parts[2]
            ip = ".".join(str(int(gw[i:i + 2], 16)) for i in (6, 4, 2, 0))
            out.append({"iface": parts[0], "ip": ip, "family": 4})
    t = read("/proc/net/ipv6_route") or ""
    for line in t.splitlines():
        parts = line.split()
        if len(parts) >= 10 and parts[0] == "00000000000000000000000000000000" \
                and parts[1] == "00" and parts[4] != "00000000000000000000000000000000":
            try:
                gw = ipaddress.IPv6Address(int(parts[4], 16)).compressed
            except ValueError:
                continue
            out.append({"iface": parts[9], "ip": gw, "family": 6})
            break
    return out


def dns_info():
    servers, search = [], []
    t = read("/etc/resolv.conf") or ""
    for line in t.splitlines():
        line = line.strip()
        if line.startswith("nameserver"):
            parts = line.split()
            if len(parts) > 1:
                servers.append(parts[1])
        elif line.startswith("search"):
            search = line.split()[1:]
    return {
        "servers": servers,
        "search": search,
        "stub": "systemd-resolved" if "127.0.0.53" in servers else None,
    }


_pub_cache = {"t": 0, "ip": None}


def public_ip():
    if "4ELEVEN_NO_PUBLIC" in os.environ:
        return None
    now = time.time()
    if now - _pub_cache["t"] < 300:
        return _pub_cache["ip"]
    for url in ("https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                ip = r.read().decode().strip()
            if ip and re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
                _pub_cache.update(t=now, ip=ip)
                return ip
        except Exception:
            continue
    _pub_cache.update(t=now, ip=None)
    return None


# ---------------------------------------------------------------------------
# dynamic metrics (CPU meter, network rates, top processes)
# ---------------------------------------------------------------------------

class CPUMeter:
    def __init__(self):
        self.prev = None
        self.prev_t = 0.0

    def sample(self):
        t = read("/proc/stat") or ""
        now = time.time()
        lines = {}
        for line in t.splitlines():
            if line.startswith("cpu"):
                parts = line.split()
                if len(parts) > 1:
                    try:
                        lines[parts[0]] = [int(x) for x in parts[1:]]
                    except ValueError:
                        pass
        total_line = lines.pop("cpu", None)
        if not total_line:
            return {"total": None, "per_core": {}}

        def pct(cur, prev):
            d = [c - p for c, p in zip(cur, prev)]
            s = sum(d)
            if s <= 0:
                return 0.0
            idle = d[3] + (d[4] if len(d) > 4 else 0)
            return round(max(0.0, min(100.0, 100 * (1 - idle / s))), 1)

        total = None
        per = {}
        if self.prev and 0 < now - self.prev_t < 10:
            total = pct(total_line, self.prev["total"])
            for k, v in lines.items():
                if k in self.prev["cores"]:
                    per[k] = pct(v, self.prev["cores"][k])
        self.prev = {"total": total_line, "cores": lines}
        self.prev_t = now
        return {"total": total, "per_core": per}


class NetMeter:
    def __init__(self):
        self.prev = {}
        self.prev_t = 0.0

    def rates(self):
        now = time.time()
        dt = now - self.prev_t
        out = {}
        for iface in _ifname_list():
            rx = read_int(f"/sys/class/net/{iface}/statistics/rx_bytes")
            tx = read_int(f"/sys/class/net/{iface}/statistics/tx_bytes")
            if iface in self.prev and dt > 0:
                out[iface] = {
                    "rx_rate": max(0, rx - self.prev[iface][0]) / dt,
                    "tx_rate": max(0, tx - self.prev[iface][1]) / dt,
                }
            else:
                out[iface] = {"rx_rate": None, "tx_rate": None}
            self.prev[iface] = (rx, tx)
        self.prev_t = now
        return out


_proc_prev = {"t": 0.0, "data": {}}


def _read_proc_stat(pid):
    s = read(f"/proc/{pid}/stat")
    if not s:
        return None
    lp = s.find("(")
    rp = s.rfind(")")
    if lp < 0 or rp <= lp:
        return None
    comm = s[lp + 1:rp]
    fields = s[rp + 2:].split()
    if len(fields) < 22:
        return None
    try:
        return {
            "comm": comm,
            "utime": int(fields[11]),
            "stime": int(fields[12]),
            "starttime": int(fields[19]),
            "rss": int(fields[21]) * PAGE_SIZE,
        }
    except ValueError:
        return None


def top_processes(limit=8, mem_total=1):
    now = time.time()
    dt = now - _proc_prev["t"] if _proc_prev["t"] else 0
    prev = _proc_prev["data"]
    rows = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        pids = []
    for pid in pids:
        st = _read_proc_stat(pid)
        if not st:
            continue
        if st["comm"].startswith("[") or st["comm"] in ("kthreadd",):
            continue
        cpu = None
        old = prev.get(pid)
        if old and dt > 0:
            ticks = (st["utime"] - old["utime"]) + (st["stime"] - old["stime"])
            cpu = round(min(99.9, ticks / dt), 1)
        rows.append({
            "pid": int(pid),
            "name": st["comm"][:40],
            "cpu": cpu,
            "mem": round(100 * st["rss"] / mem_total, 1) if mem_total else 0,
        })
    rows.sort(key=lambda r: (r["cpu"] is None, -(r["cpu"] or -1)))
    # store a fresh snapshot for the next delta calculation
    _proc_prev.update(t=now, data={})
    for pid in pids:
        st = _read_proc_stat(pid)
        if st:
            _proc_prev["data"][pid] = st
    return rows[:limit]


# ---------------------------------------------------------------------------
# payload assembly
# ---------------------------------------------------------------------------

def build_payload():
    btime = 0
    for line in (read("/proc/stat") or "").splitlines():
        if line.startswith("btime"):
            btime = int(line.split()[1])
            break
    uptime = float((read("/proc/uptime") or "0").split()[0])
    loadavg = [float(x) for x in (read("/proc/loadavg") or "0 0 0").split()[:3]]

    try:
        proc_count = sum(1 for e in os.listdir("/proc") if e.isdigit())
    except OSError:
        proc_count = 0

    cpu = cpu_info()
    freq = cpu_freq()
    mem = memory_info()
    d = dmi()
    net_rates = _net_meter.rates()

    interfaces = []
    for iface in _ifname_list():
        if iface == "lo":
            continue
        ips = []
        v4 = _ipv4_addr(iface)
        if v4:
            ips.append({"addr": v4, "family": 4, "scope": "global"})
        for v6 in _ipv6_addrs().get(iface, []):
            ips.append(v6)
        speed = read_int(f"/sys/class/net/{iface}/speed")
        rates = net_rates.get(iface, {"rx_rate": None, "tx_rate": None})
        interfaces.append({
            "name": iface,
            "state": read(f"/sys/class/net/{iface}/operstate") or "unknown",
            "mac": read(f"/sys/class/net/{iface}/address") or "",
            "mtu": read_int(f"/sys/class/net/{iface}/mtu"),
            "speed_mbps": speed if speed > 0 else None,
            "ips": ips,
            "rx_bytes": read_int(f"/sys/class/net/{iface}/statistics/rx_bytes"),
            "tx_bytes": read_int(f"/sys/class/net/{iface}/statistics/tx_bytes"),
            "rx_rate": rates["rx_rate"],
            "tx_rate": rates["tx_rate"],
        })

    env = {}
    if os.path.exists("/.dockerenv"):
        env = {"type": "container", "runtime": "docker"}
    elif os.path.exists("/run/.containerenv"):
        env = {"type": "container", "runtime": "podman"}
    try:
        cgroup = read("/proc/self/cgroup") or ""
        if not env and re.search(r"docker|containerd|kubepods|lxc", cgroup):
            env = {"type": "container", "runtime": "unknown"}
    except Exception:
        pass

    if env.get("type") == "container":
        virtualization = "container"
    elif cpu["virtualized"] or any(
            k in (d.get("product_name", "") + d.get("sys_vendor", "")).lower()
            for k in ("kvm", "virtual machine", "qemu", "vmware", "virtualbox", "bochs")):
        virtualization = "virtualized"
    else:
        virtualization = "bare metal"

    stats_cpu = _cpu_meter.sample()

    return {
        "name": NAME,
        "version": VERSION,
        "generated_at": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "server": {
            "hostname": socket.gethostname(),
            "fqdn": socket.getfqdn(),
            "os": os_pretty(),
            "kernel": platform_release(),
            "arch": platform_machine(),
            "boot_time": btime,
            "uptime": uptime,
            "loadavg": loadavg,
            "processes": proc_count,
            "python": platform_python(),
            "virtualization": virtualization,
            "cpu": cpu,
            "cpu_freq": freq,
            "memory": mem,
            "storage": storage(),
            "disks": block_devices(),
            "hardware": d,
            "gpu": gpu_info(),
            "temps": temps(),
            "network": {
                "interfaces": interfaces,
                "gateways": gateways(),
                "dns": dns_info(),
                "public_ip": public_ip(),
            },
        },
        "stats": {
            "uptime": uptime,
            "loadavg": loadavg,
            "processes": proc_count,
            "cpu": stats_cpu,
            "mem_percent": mem["percent"],
            "net": {
                "rx_rate": sum((i["rx_rate"] or 0) for i in interfaces),
                "tx_rate": sum((i["tx_rate"] or 0) for i in interfaces),
            },
            "top": top_processes(limit=8, mem_total=mem["total"]),
        },
    }


def platform_release():
    import platform
    return platform.release()


def platform_machine():
    import platform
    return platform.machine()


def platform_python():
    import platform
    return platform.python_version()


_cpu_meter = CPUMeter()
_net_meter = NetMeter()


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"{NAME}/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}\n")

    def address_string(self):
        return f"{self.client_address[0]}:{self.client_address[1]}"  # no reverse DNS

    def _authed(self):
        token = getattr(self.server, "token", None)
        if not token:
            return True
        query = ""
        if "?" in self.path:
            query = self.path.split("?", 1)[1]
        cand = None
        for part in query.split("&"):
            if part.startswith("key="):
                cand = part[4:]
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Bearer "):
            cand = hdr[7:]
        return bool(cand) and hmac.compare_digest(cand, token)

    def _send(self, code, body, ctype="application/json", head_only=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _serve_dashboard(self, head_only=False):
        here = os.path.dirname(os.path.abspath(__file__))
        html = read(os.path.join(here, "dashboard.html"))
        if html is None:
            html = ("<!doctype html><meta charset=utf-8><title>4eleven</title>"
                    "<body style='font-family:monospace;background:#0b0f1a;color:#e5e7eb'>"
                    "<h1>4eleven</h1><p>dashboard.html not found next to server.py.</p></body>")
        self._send(200, html, "text/html; charset=utf-8", head_only)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        # /healthz is a liveness probe — always open (it leaks nothing)
        if path == "/healthz":
            self._send(200, json.dumps({"status": "ok", "name": NAME, "version": VERSION}))
            return
        if not self._authed():
            self._send(401, json.dumps({"error": "unauthorized"}))
            return
        try:
            if path == "/api/info":
                self._send(200, json.dumps(build_payload(), separators=(",", ":")))
            elif path in ("/", "/index.html"):
                self._serve_dashboard()
            elif path == "/favicon.ico":
                self._send(200, FAVICON, "image/svg+xml")
            else:
                self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:  # never let one bad request kill the thread
            sys.stderr.write(f"ERROR: {e!r}\n")
            self._send(500, json.dumps({"error": "internal error"}))

    def handle(self):
        """Swallow client aborts (e.g. curl closing early on 401) quietly."""
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            pass
        except Exception:
            import traceback
            traceback.print_exc()

    def do_HEAD(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._serve_dashboard(head_only=True)
        else:
            self._send(200, "", "application/json", head_only=True)


def main():
    ap = argparse.ArgumentParser(description=f"{NAME} — the information line (v{VERSION})")
    ap.add_argument("--host", default=os.environ.get("4ELEVEN_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("4ELEVEN_PORT", "4110")))
    ap.add_argument("--password", default=os.environ.get("4ELEVEN_PASSWORD"))
    ap.add_argument("--no-public-ip", action="store_true", help="skip public IP lookup")
    args = ap.parse_args()

    if args.no_public_ip:
        os.environ["4ELEVEN_NO_PUBLIC"] = "1"

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    srv.allow_reuse_address = True
    srv.token = args.password or None

    port = srv.server_address[1]
    print(f"  ____  _____ _    ____ _   _ _____ _   _ ____  ")
    print(f" |  _ \\| ____| |  | ___| | | | ____| \\ | |  _ \\ ")
    print(f" | |_) |  _| | |  | |_ | |_| |  _| |  \\| | | | |")
    print(f" |  _ <| |___| |__|  _||  _  | |___| |\\  | |_| |")
    print(f" |_| \\_\\_____|_____|_|  |_| |_|_____|_| \\_|____/ ")
    print(f" {NAME} v{VERSION} — the information line")
    print(f" Listening on http://{args.host}:{port}")
    print(f" Dashboard:  http://localhost:{port}/")
    print(f" API:        http://localhost:{port}/api/info")
    print(f" Auth:       {'enabled (token required)' if srv.token else 'disabled'}")
    print(" Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n4eleven stopped.")


if __name__ == "__main__":
    main()
