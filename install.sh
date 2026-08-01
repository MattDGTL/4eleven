#!/usr/bin/env bash
# =============================================================================
#  4eleven (411) — the information line
#  One-command installer for the server information dashboard.
#
#  USAGE
#    curl -fsSL https://raw.githubusercontent.com/MattDGTL/4eleven/main/install.sh | sudo bash
#    curl -fsSL .../install.sh | sudo bash -s -- --port 8080 --password hunter2
#
#  Run from a local checkout (bash install.sh) it installs the local files;
#  piped from curl it downloads server.py / dashboard.html from $REPO_RAW.
#
#  Root installs go to /opt/4eleven with a systemd service.
#  Non-root installs go to ~/.local/share/4eleven and run in the background.
# =============================================================================
set -euo pipefail

VERSION="1.0.1"
REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/MattDGTL/4eleven/main}"

# ---- configurable defaults (env can override) ----
PORT="${PORT:-4110}"
HOST="${HOST:-0.0.0.0}"
PASSWORD="${PASSWORD:-}"
DEST="${DEST:-}"
OPEN_FIREWALL="${OPEN_FIREWALL:-0}"
AUTO_PYTHON="${AUTO_PYTHON:-1}"
WANT_SERVICE=1
UNINSTALL=0
PORT_SET=0; HOST_SET=0; PASSWORD_SET=0   # track explicit CLI flags

# ---- helpers ----
log()  { echo -e "\033[1;36m[4eleven]\033[0m $*"; }
warn() { echo -e "\033[1;33m[4eleven]\033[0m $*"; }
die()  { echo -e "\033[1;31m[4eleven]\033[0m ERROR: $*" >&2; exit 1; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

usage() {
  cat <<EOF
4eleven v$VERSION — install the server information dashboard.

USAGE:
  curl -fsSL https://raw.githubusercontent.com/MattDGTL/4eleven/main/install.sh | sudo bash
  curl -fsSL .../install.sh | sudo bash -s -- --port 8080 --password secret

OPTIONS:
  --port N          listen port                        (default 4110)
  --host ADDR       bind address                       (default 0.0.0.0)
  --password PW     require token ( ?key=PW  or  Authorization: Bearer PW )
  --prefix DIR      install directory (default: /opt/4eleven as root,
                    else ~/.local/share/4eleven)
  --no-service      install files only; don't start anything
  --open-firewall   open the port via ufw / firewalld if present
  --no-auto-python  don't try to install python3 if it's missing
  --uninstall       remove 4eleven (service, files, config)
  -h, --help        show this help

ENV overrides: REPO_RAW PORT HOST PASSWORD DEST OPEN_FIREWALL AUTO_PYTHON
Examples:
  curl -fsSL URL | sudo PASSWORD=secret PORT=8080 bash
  curl -fsSL URL | bash -s -- --prefix /srv/4eleven --no-service
EOF
}

# ---- argument parsing ----
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; PORT_SET=1; shift 2 ;;
    --host) HOST="$2"; HOST_SET=1; shift 2 ;;
    --password) PASSWORD="$2"; PASSWORD_SET=1; shift 2 ;;
    --prefix) DEST="$2"; shift 2 ;;
    --no-service) WANT_SERVICE=0; shift ;;
    --open-firewall) OPEN_FIREWALL=1; shift ;;
    --no-auto-python) AUTO_PYTHON=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

# detect a local checkout (files next to this script) vs piped-from-curl
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || echo "$PWD")"
IS_ROOT=0; [ "$(id -u)" = 0 ] && IS_ROOT=1

# =============================================================================
# UNINSTALL  (runs first so it can override DEST_DIR)
# =============================================================================
if [ "$UNINSTALL" = 1 ]; then
  # install.sh always lives inside its install dir — uninstall THAT dir
  DEST_DIR="$SCRIPT_DIR"
  if [ -d "$SCRIPT_DIR/.git" ]; then
    die "this looks like a source checkout (has .git) — refusing. Run --uninstall from the installed copy, or use --prefix DIR --uninstall."
  fi
  CONF_FILE="/etc/4eleven.conf"; [ "$IS_ROOT" = 1 ] || CONF_FILE="$DEST_DIR/4eleven.conf"
  log "uninstalling 4eleven from $DEST_DIR..."
  if [ "$IS_ROOT" = 1 ]; then
    if [ -f /etc/systemd/system/4eleven.service ]; then
      systemctl stop 4eleven.service 2>/dev/null || true
      systemctl disable 4eleven.service 2>/dev/null || true
      rm -f /etc/systemd/system/4eleven.service
      systemctl daemon-reload 2>/dev/null || true
    fi
    rm -rf "${DEST_DIR:?}"
    rm -f /etc/4eleven.conf
  else
    if [ -f "$DEST_DIR/4eleven.pid" ]; then
      kill "$(cat "$DEST_DIR/4eleven.pid")" 2>/dev/null || true
      rm -f "$DEST_DIR/4eleven.pid"
    fi
    rm -rf "${DEST_DIR:?}"
  fi
  log "4eleven removed."
  exit 0
fi

if [ "$IS_ROOT" = 1 ]; then DEST_DIR="${DEST:-/opt/4eleven}"
else DEST_DIR="${DEST:-${HOME}/.local/share/4eleven}"; fi
CONF_FILE="/etc/4eleven.conf"; [ "$IS_ROOT" = 1 ] || CONF_FILE="$DEST_DIR/4eleven.conf"

# =============================================================================
# CHECKS
# =============================================================================
if ! command -v python3 >/dev/null 2>&1; then
  if [ "$AUTO_PYTHON" = 1 ] && [ "$IS_ROOT" = 1 ]; then
    log "python3 not found — installing..."
    if command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -y -qq python3
    elif command -v dnf >/dev/null 2>&1; then dnf install -y python3
    elif command -v yum >/dev/null 2>&1; then yum install -y python3
    elif command -v apk >/dev/null 2>&1; then apk add --no-cache python3
    else die "could not install python3 automatically (no apt/dnf/yum/apk)"
    fi
  else
    die "python3 is required but not found. Install it or re-run with --no-auto-python after installing."
  fi
fi

# port conflict check (best effort)
if command -v ss >/dev/null 2>&1 && ss -tln "( sport = :$PORT )" 2>/dev/null | grep -q LISTEN; then
  warn "port $PORT is already in use — install anyway? continuing (use --port to change it)"
fi

# =============================================================================
# INSTALL FILES
# =============================================================================
mkdir -p "$DEST_DIR"
log "installing to $DEST_DIR"

install_file() { # $1 = filename, $2 = mode
  local name="$1" mode="$2"
  if [ -f "$SCRIPT_DIR/$name" ]; then
    install -m "$mode" "$SCRIPT_DIR/$name" "$DEST_DIR/$name"
    log "copied $name (local checkout)"
  else
    need_cmd curl
    log "downloading $name from $REPO_RAW/$name"
    curl -fsSL --connect-timeout 10 "$REPO_RAW/$name" -o "$DEST_DIR/$name" \
      || die "failed to download $name — check REPO_RAW=$REPO_RAW"
  fi
}

install_file server.py 0755
install_file dashboard.html 0644
install_file install.sh 0755
# generate uninstall.sh with the real install dir baked in
cat > "$DEST_DIR/uninstall.sh" <<EOF
#!/usr/bin/env bash
# 4eleven uninstaller (generated by install.sh)
exec bash "$DEST_DIR/install.sh" --uninstall
EOF
chmod 0755 "$DEST_DIR/uninstall.sh"

# =============================================================================
# CONFIG  (upgrade-friendly: inherit unspecified settings from existing config)
# =============================================================================
if [ -f "$CONF_FILE" ]; then
  OLD="$(sed -n 's/^4ELEVEN_PORT=//p' "$CONF_FILE" | tail -1)"
  if [ "$PORT_SET" = 0 ] && [ -n "$OLD" ]; then PORT="$OLD"; fi
  OLD="$(sed -n 's/^4ELEVEN_HOST=//p' "$CONF_FILE" | tail -1)"
  if [ "$HOST_SET" = 0 ] && [ -n "$OLD" ]; then HOST="$OLD"; fi
  OLD="$(sed -n 's/^4ELEVEN_PASSWORD=//p' "$CONF_FILE" | tail -1)"
  if [ "$PASSWORD_SET" = 0 ] && [ -n "$OLD" ]; then PASSWORD="$OLD"; fi
  log "existing config found — inheriting from $CONF_FILE (explicit flags override)"
fi
mkdir -p "$(dirname "$CONF_FILE")"
cat > "$CONF_FILE" <<EOF
# 4eleven configuration (generated $(date -u +%Y-%m-%dT%H:%M:%SZ))
4ELEVEN_HOST=$HOST
4ELEVEN_PORT=$PORT
4ELEVEN_PASSWORD=$PASSWORD
EOF
if [ -n "$PASSWORD" ]; then chmod 600 "$CONF_FILE"; log "auth enabled (token required)"; fi
log "config written to $CONF_FILE"

# =============================================================================
# SERVICE / LAUNCH
# =============================================================================
if [ "$WANT_SERVICE" = 0 ]; then
  log "files installed — start manually with:"
  echo "    python3 $DEST_DIR/server.py --port $PORT --host $HOST"
elif [ "$IS_ROOT" = 1 ] && command -v systemctl >/dev/null 2>&1; then
  cat > /etc/systemd/system/4eleven.service <<EOF
[Unit]
Description=4eleven — server information dashboard (411)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/env python3 $DEST_DIR/server.py
EnvironmentFile=$CONF_FILE
Restart=always
RestartSec=3
PrivateTmp=true
Nice=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable 4eleven.service >/dev/null 2>&1 || true
  systemctl restart 4eleven.service
  log "systemd service installed and started (4eleven.service)"
else
  # no systemd (or non-root): run in the background
  if [ -f "$DEST_DIR/4eleven.pid" ] && kill -0 "$(cat "$DEST_DIR/4eleven.pid")" 2>/dev/null; then
    warn "an existing 4eleven is running (pid $(cat "$DEST_DIR/4eleven.pid")) — restarting it"
    kill "$(cat "$DEST_DIR/4eleven.pid")" 2>/dev/null || true
    sleep 1
  fi
  set -- \
    --host "$(sed -n 's/^4ELEVEN_HOST=//p' "$CONF_FILE" | tail -1)" \
    --port "$(sed -n 's/^4ELEVEN_PORT=//p' "$CONF_FILE" | tail -1)"
  PW_V="$(sed -n 's/^4ELEVEN_PASSWORD=//p' "$CONF_FILE" | tail -1)"
  [ -n "$PW_V" ] && set -- "$@" --password "$PW_V"
  setsid nohup python3 "$DEST_DIR/server.py" "$@" >> "$DEST_DIR/4eleven.log" 2>&1 &
  echo $! > "$DEST_DIR/4eleven.pid"
  log "started in background (pid $(cat "$DEST_DIR/4eleven.pid"))"
  if [ "$IS_ROOT" != 1 ]; then
    warn "non-root install without systemd — not boot-persistent. Add to your shell profile or cron:"
    echo "    @reboot setsid nohup python3 $DEST_DIR/server.py >> $DEST_DIR/4eleven.log 2>&1 &"
  fi
fi

# =============================================================================
# FIREWALL
# =============================================================================
if [ "$OPEN_FIREWALL" = 1 ] && [ "$IS_ROOT" = 1 ]; then
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "$PORT"/tcp >/dev/null 2>&1 && log "ufw: allowed $PORT/tcp"
  elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="$PORT"/tcp >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
    log "firewalld: allowed $PORT/tcp"
  else
    warn "no active firewall detected — open port $PORT/tcp yourself if needed"
  fi
fi

# =============================================================================
# VERIFY + SUMMARY
# =============================================================================
READY=0
for _ in $(seq 1 15); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then READY=1; break; fi
  sleep 1
done
if [ "$READY" = 1 ]; then
  log "4eleven v$VERSION is UP — health check passed ✅"
else
  warn "health check not confirmed yet — inspect $DEST_DIR/4eleven.log"
fi

echo
echo "  ┌──────────────────────────────────────────────────────────┐"
echo "  │  4eleven · 411 = information · the information line      │"
echo "  └──────────────────────────────────────────────────────────┘"
echo
echo "  Dashboard:  http://127.0.0.1:$PORT/"
IPS="$(hostname -I 2>/dev/null || true)"
if [ -z "$IPS" ] && command -v ip >/dev/null 2>&1; then
  IPS="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | tr '\n' ' ')"
fi
if [ -z "$IPS" ]; then
  IPS="$(python3 -c 'import socket
s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("8.8.8.8", 80)); print(s.getsockname()[0])
except Exception: pass' 2>/dev/null || true)"
fi
if [ -n "$IPS" ]; then
  for ip in $IPS; do
    case "$ip" in *:*) echo "  Network:    http://[$ip]:$PORT/" ;; *) echo "  Network:    http://$ip:$PORT/" ;; esac
  done
fi
echo "  API:        http://127.0.0.1:$PORT/api/info"
echo
echo "  Themes:     dark · light · cartoon · futuristic · 8-bit (click top-right)"
echo "  Config:     $CONF_FILE"
if [ -n "$PASSWORD" ]; then
  echo "  Auth:       use  ?key=$PASSWORD   or   Authorization: Bearer $PASSWORD"
fi
if [ "$IS_ROOT" = 1 ] && command -v systemctl >/dev/null 2>&1; then
  echo "  Manage:     systemctl {status|restart|stop|enable} 4eleven"
fi
echo "  Uninstall:  bash $DEST_DIR/uninstall.sh   (or re-run installer with --uninstall)"
echo
