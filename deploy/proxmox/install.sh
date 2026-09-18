#!/usr/bin/env bash
# Native (no Docker) install of the timekpr-next-hub onto a Debian LXC:
# Postgres from apt, the hub as a systemd unit against a uv-managed venv.
# Run as root, inside the container (see create-lxc.sh for the host side).
#
# Idempotent: re-running this script on an existing install upgrades the
# code and reruns migrations without touching the parent account, the
# enrolled devices, or the DB password. Safe defaults, explicit flags for
# the rest -- see --help.
set -euo pipefail

# -------------------------------------------------------------- constants --
PREFIX=/opt/timekpr-hub
ENVDIR=/etc/timekpr-hub
ENVFILE="$ENVDIR/hub.env"
BACKUP_DIR=/var/backups/timekpr-hub
UV_VERSION=0.12.10
DB_NAME=timekpr_hub
DB_USER=timekpr_hub

SCRIPT_DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
# Works whether this is run from a git checkout or an extracted release
# tarball -- either way deploy/proxmox/install.sh sits two levels under
# the repo root.
SRC_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)

HUB_TZ=UTC
BIND=0.0.0.0
PORT=8000
SKIP_POSTGRES=0
NO_START=0

# ---------------------------------------------------------------- helpers --
log()  { printf '==> %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: install.sh [options]

  --tz TZ             HUB_TZ to configure (default: UTC)
  --bind ADDR         address uvicorn binds (default: 0.0.0.0)
  --port PORT         port uvicorn binds (default: 8000)
  --skip-postgres     don't touch Postgres (assume it's already tuned/running)
  --no-start          install everything but don't enable/start the units
  -h, --help          show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tz) HUB_TZ=$2; shift 2 ;;
    --bind) BIND=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --skip-postgres) SKIP_POSTGRES=1; shift ;;
    --no-start) NO_START=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run as root"

UPGRADE=0
[[ -d "$PREFIX/venv" ]] && UPGRADE=1

# ----------------------------------------------------------- 1. packages --
# Set before apt: Debian container templates often generate only C/POSIX,
# and postgresql-common's postinst creates the cluster using the ambient
# locale -- an unset locale here yields a SQL_ASCII cluster that mangles
# non-ASCII usernames and sorts oddly. C.UTF-8 needs no locale-gen (it's
# built into glibc).
export LANG=C.UTF-8 LC_ALL=C.UTF-8 DEBIAN_FRONTEND=noninteractive

if [[ $SKIP_POSTGRES -eq 0 ]]; then
  log "installing packages (postgresql, uv's runtime deps, rsync)"
  apt-get update -qq
  apt-get install -y --no-install-recommends \
    postgresql postgresql-client ca-certificates curl tzdata rsync >/dev/null
else
  log "installing packages (--skip-postgres: no postgresql package)"
  apt-get update -qq
  apt-get install -y --no-install-recommends ca-certificates curl tzdata rsync >/dev/null
fi

# ------------------------------------------------------------------ 2. uv --
# Pinned to the same version as hub/Dockerfile's builder stage, so both
# deployment paths resolve uv.lock identically. No compiler needed: every
# runtime dependency ships a manylinux wheel for the interpreters this
# targets (see deploy/proxmox/README.md's "Risks" section) -- deliberately
# not installing build-essential/python3-dev.
if ! /usr/local/bin/uv --version 2>/dev/null | grep -q "$UV_VERSION"; then
  log "installing uv $UV_VERSION"
  arch=$(uname -m)
  case "$arch" in
    x86_64) uv_target=x86_64-unknown-linux-gnu ;;
    aarch64) uv_target=aarch64-unknown-linux-gnu ;;
    *) die "unsupported architecture: $arch" ;;
  esac
  tmpdir=$(mktemp -d)
  trap 'rm -rf "$tmpdir"' EXIT
  # Keep the release's own filename -- the .sha256 file's content names it,
  # so renaming it (as this used to, to uv.tar.gz) makes `sha256sum -c` look
  # for a file that no longer exists.
  uv_archive="uv-$uv_target.tar.gz"
  curl -fsSL -o "$tmpdir/$uv_archive" \
    "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$uv_archive"
  curl -fsSL -o "$tmpdir/$uv_archive.sha256" \
    "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$uv_archive.sha256"
  (cd "$tmpdir" && sha256sum -c "$uv_archive.sha256")
  tar -xzf "$tmpdir/$uv_archive" -C "$tmpdir"
  install -m0755 "$tmpdir/uv-$uv_target/uv" "$tmpdir/uv-$uv_target/uvx" /usr/local/bin/
  rm -rf "$tmpdir"
  trap - EXIT
else
  log "uv $UV_VERSION already installed"
fi

# --------------------------------------------------- 3. service user/dirs --
log "creating the timekpr-hub service user and directories"
install -Dm0644 "$SCRIPT_DIR/sysusers.d/timekpr-hub.conf" /usr/lib/sysusers.d/timekpr-hub.conf
install -Dm0644 "$SCRIPT_DIR/tmpfiles.d/timekpr-hub.conf" /usr/lib/tmpfiles.d/timekpr-hub.conf
systemd-sysusers timekpr-hub.conf
systemd-tmpfiles --create timekpr-hub.conf

# ------------------------------------------------------------- 4. postgres --
if [[ $SKIP_POSTGRES -eq 0 ]]; then
  log "checking the Postgres cluster"
  pgver=$(pg_lsclusters -h | awk 'NR==1 {print $1}')
  [[ -n "$pgver" ]] || die "no Postgres cluster found after installing postgresql"

  encoding=$(su -s /bin/sh postgres -c "psql -tAc \"show server_encoding\"" 2>/dev/null || true)
  if [[ "$encoding" != "UTF8" ]]; then
    has_user_dbs=$(su -s /bin/sh postgres -c \
      "psql -tAc \"select count(*) from pg_database where datname not in ('postgres','template0','template1')\"")
    if [[ "${has_user_dbs:-0}" -gt 0 ]]; then
      die "Postgres cluster $pgver/main has encoding '$encoding' (need UTF8) and already has" \
          "user databases -- dump them, pg_dropcluster/pg_createcluster --locale=C.UTF-8 -e UTF8," \
          "then restore, before re-running this script"
    fi
    log "recreating cluster $pgver/main with UTF8/C.UTF-8 (it was empty and '$encoding')"
    pg_dropcluster --stop "$pgver" main
    pg_createcluster --locale=C.UTF-8 -e UTF8 --start "$pgver" main
  fi

  conf_dir="/etc/postgresql/$pgver/main/conf.d"
  main_conf="/etc/postgresql/$pgver/main/postgresql.conf"
  mkdir -p "$conf_dir"
  grep -q "^include_dir = 'conf.d'" "$main_conf" || echo "include_dir = 'conf.d'" >> "$main_conf"

  tuning_changed=0
  if ! cmp -s "$SCRIPT_DIR/postgresql/10-timekpr-hub.conf" "$conf_dir/10-timekpr-hub.conf" 2>/dev/null; then
    tuning_changed=1
  fi
  install -Dm0644 "$SCRIPT_DIR/postgresql/10-timekpr-hub.conf" "$conf_dir/10-timekpr-hub.conf"

  systemctl enable --now postgresql >/dev/null
  if [[ $tuning_changed -eq 1 ]]; then
    log "Postgres tuning changed -- restarting cluster $pgver/main"
    systemctl restart "postgresql@$pgver-main"
  fi
else
  log "skipping Postgres setup (--skip-postgres)"
fi

# ---------------------------------------------- 5. database role/password --
# Reuse the existing password on every re-run -- never regenerate it, or
# every upgrade would lock the hub out of its own database.
if [[ -f "$ENVFILE" ]] && grep -q '^DATABASE_URL=' "$ENVFILE"; then
  db_pass=$(sed -n 's#^DATABASE_URL=postgresql+asyncpg://[^:]*:\([^@]*\)@.*#\1#p' "$ENVFILE")
  [[ -n "$db_pass" ]] || die "$ENVFILE exists but DATABASE_URL couldn't be parsed"
  log "reusing the existing database password from $ENVFILE"
else
  # Alphanumeric only -- keeps the password safe to embed in a DATABASE_URL
  # DSN with no URL-escaping. head via process substitution, not a pipe:
  # `tr | head -c32` gets SIGPIPE'd once head is satisfied, and pipefail
  # would turn that into a fatal `set -e` abort right here.
  db_pass=$(head -c 32 <(tr -dc 'A-Za-z0-9' </dev/urandom))
  log "generated a new database password"
fi

if [[ $SKIP_POSTGRES -eq 0 ]]; then
  role_exists=$(su -s /bin/sh postgres -c \
    "psql -tAc \"select 1 from pg_roles where rolname='$DB_USER'\"")
  if [[ "$role_exists" != "1" ]]; then
    log "creating role $DB_USER"
    su -s /bin/sh postgres -c "psql -c \"create role $DB_USER login\""
  fi
  # Always re-set the password so the role and hub.env can never drift.
  su -s /bin/sh postgres -c "psql -c \"alter role $DB_USER password '$db_pass'\""

  db_exists=$(su -s /bin/sh postgres -c \
    "psql -tAc \"select 1 from pg_database where datname='$DB_NAME'\"")
  if [[ "$db_exists" != "1" ]]; then
    log "creating database $DB_NAME"
    su -s /bin/sh postgres -c "createdb -O $DB_USER $DB_NAME"
  fi
fi

# --------------------------------------------------------- 6. env file ----
log "writing $ENVFILE"
tmp_env=$(mktemp)
cat > "$tmp_env" <<EOF
DATABASE_URL=postgresql+asyncpg://$DB_USER:$db_pass@127.0.0.1:5432/$DB_NAME
HUB_TZ=$HUB_TZ
TIMEKPR_HUB_LOG_LEVEL=INFO
# Uncomment to change how often agents (and open dashboard tabs) poll --
# default is 20000 (20s). The stale/offline threshold is 3x this value.
#DEFAULT_NEXT_POLL_MS=20000
EOF
install -m0640 -o root -g timekpr-hub "$tmp_env" "$ENVFILE"
rm -f "$tmp_env"

# ---------------------------------------- 7. pre-upgrade safety dump/stop --
if [[ $UPGRADE -eq 1 ]]; then
  if [[ $SKIP_POSTGRES -eq 0 ]]; then
    log "taking a pre-upgrade safety dump into $BACKUP_DIR"
    dump_file="$BACKUP_DIR/${DB_NAME}_pre-upgrade_$(date +%Y%m%d%H%M%S).dump"
    su -s /bin/sh postgres -c "pg_dump -Fc -d $DB_NAME -f $dump_file.tmp"
    mv -f "$dump_file.tmp" "$dump_file"
  fi
  if systemctl is-active --quiet timekpr-hub; then
    log "stopping timekpr-hub before upgrading the venv"
    systemctl stop timekpr-hub
  fi
fi

# -------------------------------------------------------- 8. code + venv --
log "syncing source to $PREFIX/src"
mkdir -p "$PREFIX/src"
# Mirrors hub/Dockerfile's builder COPY set exactly, so both deployment
# paths install identical code from identical inputs. .python-version is
# excluded on purpose: it names 3.12, but this installs against whatever
# python3 the LXC actually has (often newer) -- see README.md's "Risks".
# Order matters for rsync filters: the first matching rule wins, so the
# junk excludes must come before the tree includes they'd otherwise get
# swept up by.
rsync -a --delete \
  --exclude='.venv/' --exclude='__pycache__/' --exclude='*.pyc' --exclude='.python-version' \
  --include='/pyproject.toml' --include='/uv.lock' \
  --include='/core/' --include='/core/**' \
  --include='/hub/' --include='/hub/**' \
  --include='/agent/' --include='/agent/pyproject.toml' \
  --exclude='*' \
  "$SRC_ROOT/" "$PREFIX/src/"

venv_py=""
[[ -x "$PREFIX/venv/bin/python" ]] && venv_py=$("$PREFIX/venv/bin/python" -c \
  'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)
sys_py=$(/usr/bin/python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if [[ -n "$venv_py" && "$venv_py" != "$sys_py" ]]; then
  log "system python changed ($venv_py -> $sys_py) -- rebuilding the venv"
  rm -rf "$PREFIX/venv"
fi

log "uv sync --locked --no-dev --package timekpr-hub"
UV_PROJECT_ENVIRONMENT="$PREFIX/venv" \
UV_PYTHON_DOWNLOADS=never \
UV_CACHE_DIR=/var/cache/uv \
  /usr/local/bin/uv sync --locked --no-dev --package timekpr-hub --no-editable \
    --python /usr/bin/python3 --project "$PREFIX/src"

chown -R root:root "$PREFIX"
chmod -R go-w "$PREFIX"

# ------------------------------------------------------------- 9. units ---
log "installing systemd units"
install -Dm0644 "$SCRIPT_DIR/systemd/timekpr-hub-migrate.service" \
  /etc/systemd/system/timekpr-hub-migrate.service
install -Dm0644 "$SCRIPT_DIR/systemd/timekpr-hub.service" \
  /etc/systemd/system/timekpr-hub.service

# --bind/--port aren't in the shipped unit (it hardcodes 0.0.0.0:8000, the
# common case) -- patch a drop-in only when either was overridden.
if [[ "$BIND" != "0.0.0.0" || "$PORT" != "8000" ]]; then
  mkdir -p /etc/systemd/system/timekpr-hub.service.d
  cat > /etc/systemd/system/timekpr-hub.service.d/override.conf <<EOF
[Service]
ExecStart=
ExecStart=/opt/timekpr-hub/venv/bin/uvicorn timekpr_hub.app:app --host $BIND --port $PORT
EOF
fi

systemctl daemon-reload

if [[ $NO_START -eq 1 ]]; then
  log "--no-start given: units installed but not enabled/started"
else
  if [[ $UPGRADE -eq 1 ]]; then
    # timekpr-hub-migrate is RemainAfterExit=yes, so a plain `start` after
    # the first successful run is a no-op -- an upgrade needs an explicit
    # restart to actually apply new migrations.
    log "restarting migrations"
    systemctl restart timekpr-hub-migrate
  else
    systemctl enable --now timekpr-hub-migrate
  fi
  systemctl enable --now timekpr-hub

  log "waiting for /healthz"
  ok=0
  for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 1
  done
  [[ $ok -eq 1 ]] || die "hub did not come up -- check: journalctl -u timekpr-hub -u timekpr-hub-migrate"
fi

echo
log "done. Hub listening on $BIND:$PORT"
if [[ $UPGRADE -eq 0 ]]; then
  log "CLAIM IT NOW: open http://<this box>:$PORT/setup and create the first parent" \
      "account -- until you do, anyone who can reach the hub can create it themselves."
fi
