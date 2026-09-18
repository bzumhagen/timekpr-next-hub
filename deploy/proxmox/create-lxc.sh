#!/usr/bin/env bash
# Creates the LXC and runs install.sh inside it. Run this on the Proxmox
# host itself (where `pct` exists) -- either from a checkout of this repo,
# or standalone (e.g. `curl -fsSL .../create-lxc.sh | bash`, community-
# scripts style), in which case the source is fetched from GitHub instead
# of pushed from a local tree.
#
# Geometry rationale (see README.md for the full writeup): unprivileged,
# no nesting/keyctl (those exist for Docker-in-LXC, which this deployment
# deliberately doesn't use), 1GB RAM as a ceiling (steady state sits near
# 300MB; the rest just needs to be available during `apt`/`uv sync`).
set -euo pipefail

CTID=${CTID:-}
# Not named HOSTNAME -- bash pre-populates that to the host's own hostname
# (e.g. "pve"), so `ask` would see it as already-set and skip our default.
CT_HOSTNAME=${CT_HOSTNAME:-}
TEMPLATE_STORAGE=${TEMPLATE_STORAGE:-}
# Debian ships new point releases regularly, so pinning an exact filename
# here would go stale. TEMPLATE, if set, is used as-is (storage:vztmpl/file);
# otherwise the newest debian-13-standard build is resolved at runtime below.
TEMPLATE=${TEMPLATE:-}
STORAGE=${STORAGE:-}
BRIDGE=${BRIDGE:-}
CORES=${CORES:-}
MEMORY_MB=${MEMORY_MB:-}
SWAP_MB=${SWAP_MB:-}
DISK_GB=${DISK_GB:-}
NET_CONF=${NET_CONF:-}
HUB_TZ=${HUB_TZ:-}
REPO_URL=${REPO_URL:-https://github.com/bzumhagen/timekpr-next-hub}
REF=${REF:-main}

detect_tz() {
  local tz
  if command -v timedatectl >/dev/null && tz=$(timedatectl show -p Timezone --value 2>/dev/null) && [ -n "$tz" ]; then
    echo "$tz"
  elif [ -r /etc/timezone ]; then
    cat /etc/timezone
  else
    echo UTC
  fi
}

log() { printf '==> %s\n' "$*" >&2; }

command -v pct >/dev/null || {
  echo "error: pct not found -- run this on the Proxmox host, not inside a container" >&2
  exit 1
}

# Prompt for anything not already set via environment variable, so this
# still works run standalone (`curl | bash`, community-scripts style)
# without an interactive terminal, but offers overrides when there is one.
# Reads from /dev/tty rather than stdin since stdin is the curl pipe itself
# in that mode.
interactive=1
[ -z "${NONINTERACTIVE:-}" ] && [ -r /dev/tty ] || interactive=0
ask() {
  local __var=$1 __text=$2 __def=$3
  local -n __ref=$__var
  if [ "$interactive" -eq 1 ] && [ -z "${__ref:-}" ]; then
    local __ans
    read -rp "$__text [$__def]: " __ans < /dev/tty > /dev/tty
    __ref=${__ans:-$__def}
  else
    __ref=${__ref:-$__def}
  fi
}

ask CTID "Container ID" "$(pvesh get /cluster/nextid)"
ask CT_HOSTNAME "Hostname" "timekpr-hub"
ask TEMPLATE_STORAGE "Template storage" "local"
ask STORAGE "Rootfs storage" "local-lvm"
ask BRIDGE "Network bridge" "vmbr0"
ask CORES "CPU cores" "2"
ask MEMORY_MB "Memory (MB)" "1024"
ask SWAP_MB "Swap (MB)" "512"
ask DISK_GB "Disk (GB)" "8"
ask HUB_TZ "Timezone" "$(detect_tz)"
NET_CONF=${NET_CONF:-"name=eth0,bridge=$BRIDGE,ip=dhcp"}

if [ -z "$TEMPLATE" ]; then
  # Prefer a build already downloaded on this storage; otherwise ask the
  # Proxmox template repo for the newest debian-13-standard build and
  # download it. Sorted with `sort -V` so e.g. 13.10-1 sorts after 13.6-1.
  TEMPLATE_FILE=$(pveam list "$TEMPLATE_STORAGE" 2>/dev/null \
    | awk '{print $1}' | grep -oE 'debian-13-standard_[0-9.-]+_amd64\.tar\.zst' \
    | sort -V | tail -n1)
  if [ -z "$TEMPLATE_FILE" ]; then
    pveam update
    TEMPLATE_FILE=$(pveam available --section system \
      | awk '{print $1}' | grep -E '^debian-13-standard_[0-9.-]+_amd64\.tar\.zst$' \
      | sort -V | tail -n1)
    [ -n "$TEMPLATE_FILE" ] || {
      echo "error: no debian-13-standard template found in 'pveam available' -- set TEMPLATE explicitly" >&2
      exit 1
    }
    log "downloading template $TEMPLATE_FILE to $TEMPLATE_STORAGE"
    pveam download "$TEMPLATE_STORAGE" "$TEMPLATE_FILE"
  fi
  TEMPLATE="$TEMPLATE_STORAGE:vztmpl/$TEMPLATE_FILE"
else
  TEMPLATE_STORAGE=${TEMPLATE%%:*}
  TEMPLATE_FILE=${TEMPLATE#*:vztmpl/}
  if ! pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -qF "$TEMPLATE_FILE"; then
    log "template $TEMPLATE_FILE not found on $TEMPLATE_STORAGE -- downloading"
    pveam update
    pveam download "$TEMPLATE_STORAGE" "$TEMPLATE_FILE"
  fi
fi
log "using template $TEMPLATE"

if pct status "$CTID" >/dev/null 2>&1; then
  log "CT $CTID already exists -- reusing it (sizing/template flags are ignored on reuse; install.sh below is what's idempotent)"
else
  log "creating CT $CTID ($CT_HOSTNAME)"
  pct create "$CTID" "$TEMPLATE" \
    --hostname "$CT_HOSTNAME" \
    --unprivileged 1 \
    --features '' \
    --cores "$CORES" \
    --memory "$MEMORY_MB" \
    --swap "$SWAP_MB" \
    --rootfs "$STORAGE:$DISK_GB" \
    --onboot 1 \
    --net0 "$NET_CONF"
fi

if [ "$(pct status "$CTID")" = "status: running" ]; then
  log "CT $CTID already running"
else
  log "starting CT $CTID"
  pct start "$CTID"
fi

log "waiting for networking"
for _ in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done

# Only trust a local checkout when this script is actually sitting inside
# one -- e.g. run standalone via `curl | bash`, $0 is a throwaway temp file
# with no repo around it, and walking ".."s from it would tar up unrelated
# (or, worse, arbitrary) host directories.
REPO_ROOT=""
script_dir=$(dirname "$(readlink -f "$0")")
if candidate=$(cd "$script_dir" && git rev-parse --show-toplevel 2>/dev/null) \
  && [ -e "$candidate/deploy/proxmox/install.sh" ]; then
  REPO_ROOT="$candidate"
fi

if [ -n "$REPO_ROOT" ]; then
  log "pushing local checkout ($REPO_ROOT) into CT $CTID:/root/timekpr-next-hub"
  pct exec "$CTID" -- mkdir -p /root/timekpr-next-hub
  # tar over a pipe rather than `pct push` (which is one-file-at-a-time) --
  # far fewer round trips for a whole tree. --one-file-system is a belt-and-
  # suspenders guard against ever sweeping up /proc, /sys, etc.
  tar -C "$REPO_ROOT" --one-file-system \
    --exclude=.git --exclude='**/__pycache__' --exclude='.venv' --exclude='agent/.venv' \
    --exclude='dist' --exclude='build' -cf - . | pct exec "$CTID" -- tar -C /root/timekpr-next-hub -xf -
else
  log "no local checkout found -- fetching $REPO_URL@$REF into CT $CTID"
  pct exec "$CTID" -- bash -c "
    set -euo pipefail
    export LANG=C.UTF-8 LC_ALL=C.UTF-8 DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y --no-install-recommends ca-certificates curl >/dev/null
    mkdir -p /root/timekpr-next-hub
    curl -fsSL '$REPO_URL/archive/$REF.tar.gz' \
      | tar -xzf - -C /root/timekpr-next-hub --strip-components=1
  "
fi

log "running install.sh inside CT $CTID"
pct exec "$CTID" -- bash /root/timekpr-next-hub/deploy/proxmox/install.sh --tz "$HUB_TZ" "$@"

ip=$(pct exec "$CTID" -- hostname -I | awk '{print $1}')
log "done. Hub should be reachable at http://$ip:8000 -- claim it at /setup now."
