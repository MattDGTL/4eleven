#!/usr/bin/env bash
# 4eleven - canonical verification. Run: bash test.sh
# Covers: syntax, formatter algorithm, dashboard essentials, installer
# config inheritance, live server smoke test. Exit 0 = all green.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PASS=0; FAIL=0
ok(){ echo "  ok   $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL $1"; FAIL=$((FAIL+1)); }
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

echo "== syntax =="
if python3 -m py_compile server.py; then ok "server.py compiles"; else bad "server.py compiles"; fi
if bash -n install.sh; then ok "install.sh parses"; else bad "install.sh parses"; fi

echo "== formatter (fmtBytes) =="
python3 - <<'PY'
def f(n):
    if n < 1024: return str(round(n)) + " B"
    u = ["KiB","MiB","GiB","TiB","PiB"]; i = -1
    while True:
        n /= 1024; i += 1
        if not (n >= 1024 and i < len(u) - 1): break
    return (f"{n:.2f}" if n < 10 else f"{n:.1f}" if n < 100 else f"{n:.0f}") + " " + u[i]
cases = [(34.886234578986006, "35 B"), (720.5019909165757, "721 B"), (4096, "4.00 KiB"),
         (82.3e9, "76.6 GiB"), (12000.1e9, "10.9 TiB"), (0, "0 B"), (1024**5*3.5, "3.50 PiB")]
bad = [c for c in cases if f(c[0]) != c[1]]
for v, w in cases:
    print(f"  {'ok   ' if f(v) == w else 'FAIL '} fmtBytes({v}) = {f(v)!r} (want {w!r})")
raise SystemExit(1 if bad else 0)
PY
if [ $? = 0 ]; then ok "formatter matches expected outputs"; else bad "formatter matches expected outputs"; fi

echo "== dashboard essentials =="
grep -q 'u = \["KiB"' dashboard.html && ok "binary units" || bad "binary units"
grep -q 'tile.wide' dashboard.html && ok "wide tiles" || bad "wide tiles"
grep -q 'text-overflow: ellipsis' dashboard.html && ok "overflow guard" || bad "overflow guard"
grep -q 'id="iface-toggle"' dashboard.html && ok "interface collapse toggle" || bad "interface collapse toggle"
grep -q 'max-height: 480px; overflow-y: auto' dashboard.html && ok "interface scroll cap" || bad "interface scroll cap"

echo "== security =="
grep -q 'Content-Security-Policy' server.py && ok "CSP header set" || bad "CSP header set"
grep -q 'X-Frame-Options' server.py && ok "X-Frame-Options header set" || bad "X-Frame-Options header set"
grep -q 'Referrer-Policy' server.py && ok "Referrer-Policy header set" || bad "Referrer-Policy header set"
grep -q 'class RateLimiter' server.py && ok "rate limiter present" || bad "rate limiter present"
grep -q 'const esc = ' dashboard.html && ok "HTML escaping helper present" || bad "HTML escaping helper present"
grep -q 'esc(m.mount)' dashboard.html && ok "escaping applied to storage mounts" || bad "escaping applied to storage mounts"
grep -q 'esc(i.name)' dashboard.html && ok "escaping applied to interface names" || bad "escaping applied to interface names"

P2=$((5300 + RANDOM % 300))
python3 server.py --port "$P2" --no-public-ip --rate-limit 5 >"$T/srv2.log" 2>&1 &
SRV2=$!
up=0
for _ in $(seq 1 15); do curl -fsS -m 1 "http://127.0.0.1:$P2/healthz" >/dev/null 2>&1 && { up=1; break; }; sleep 0.3; done
codes=""
for _ in $(seq 1 10); do codes="$codes $(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$P2/healthz")"; done
kill "$SRV2" 2>/dev/null
if echo "$codes" | grep -q 429; then ok "rate limit returns 429 when exceeded ($codes)"; else bad "rate limit returns 429 when exceeded ($codes)"; fi

echo "== installer config inheritance =="
bash install.sh --prefix "$T" --no-service --port 4115 --password s1 >/dev/null 2>&1
bash install.sh --prefix "$T" --no-service --port 4116 >/dev/null 2>&1
if grep -q '4ELEVEN_PASSWORD=s1' "$T/4eleven.conf" && grep -q '4ELEVEN_PORT=4116' "$T/4eleven.conf"; then
  ok "per-key inheritance (port-only re-run keeps password)"
else
  bad "per-key inheritance (port-only re-run keeps password)"
fi

echo "== live server smoke =="
P=$((5200 + RANDOM % 300))
python3 server.py --port "$P" --no-public-ip >"$T/srv.log" 2>&1 &
SRV=$!
up=0
for _ in $(seq 1 20); do
  curl -fsS -m 1 "http://127.0.0.1:$P/healthz" >/dev/null 2>&1 && { up=1; break; }
  sleep 0.3
done
if [ "$up" = 1 ]; then ok "server boots + /healthz"; else bad "server boots + /healthz"; fi
if curl -fsS -m 2 "http://127.0.0.1:$P/api/info" -o "$T/api.json" 2>/dev/null \
   && python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
assert d['name'] == '4eleven' and d['server']['hostname'] and d['server']['cpu']['logical'] > 0
print('  ok   api schema')" "$T/api.json" >/dev/null 2>&1; then
  ok "api schema sane"
else
  bad "api schema sane"
fi
kill "$SRV" 2>/dev/null

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
