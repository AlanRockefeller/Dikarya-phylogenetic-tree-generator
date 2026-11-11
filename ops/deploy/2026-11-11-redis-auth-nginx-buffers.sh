#!/bin/bash
# Root deploy for the 2026-11-11 hardening pass. Run as root:
#
#     bash /var/www/dikarya/ops/deploy/2026-11-11-redis-auth-nginx-buffers.sh            # everything
#     bash /var/www/dikarya/ops/deploy/2026-11-11-redis-auth-nginx-buffers.sh --dry-run  # show only
#
# Steps (each is idempotent; re-running after a partial failure is safe):
#
#   1. archive-dirs  var/logs/api-responses gets setgid (2775) so a dated folder
#                    created by ANY account inherits group dikarya. Without it a
#                    tree-run script made 2026-11-08 tree:tree and every web and
#                    worker archive that day failed with PermissionError. Also
#                    repairs the folders that already lost the group.
#   2. nginx         installs ops/nginx/dikarya (bigger proxy/body buffers),
#                    `nginx -t`, reload; restores the old file if the test fails.
#   3. redis-auth    Redis on 127.0.0.1 had no password on a host with ~25 shell
#                    accounts, and RQ unpickles job payloads, so any local user
#                    could run code as dikarya (or CONFIG SET dir/dbfilename to
#                    write files as redis). Sets requirepass (live, no Redis
#                    restart, no data loss), writes REDIS_URL into the service
#                    environment, a 0600 copy for the tree account, and restarts
#                    every Dikarya service so they reconnect with it.
#
# Step 3 restarts the workers, so it REFUSES to start while any job is queued
# or running (exit 75): wait and re-run. Expect a few seconds of 5xx between
# the password going live and the web restart finishing.
#
# Rollback for step 3 (as root):
#     redis-cli -a "$PW" CONFIG SET requirepass ""        # PW from the env file
#     cp the *.bak-<stamp> backups printed below back over the originals
#     systemctl restart dikarya-worker dikarya-worker-bulk dikarya-metrics
#     /usr/local/sbin/restart-dikarya-web
set -euo pipefail

DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1
[[ $# -gt 1 || ( $# -eq 1 && $DRY -eq 0 ) ]] && { echo "usage: $0 [--dry-run]" >&2; exit 64; }
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 77; }

APP=/var/www/dikarya
ENV_FILE=/etc/dikarya/dikarya.environment.live
REDIS_CONF=/etc/redis/redis.conf
TREE_ENV=/home/tree/.dikarya/redis.env
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

say()  { printf '\n== %s\n' "$*"; }
run()  { if (( DRY )); then printf '   [dry-run] %s\n' "$*"; else "$@"; fi; }
backup() { run cp -a "$1" "$1.bak-$STAMP"; echo "   backup: $1.bak-$STAMP"; }

# --------------------------------------------------------------------------
say "1. archive directories"
ARCH=$APP/var/logs/api-responses
run chgrp dikarya "$ARCH"
run chmod 2775 "$ARCH"
# Dated folders: shared group, setgid, group rwx; files group rw. Only fix
# what is wrong, so mtimes of healthy folders are left alone.
for d in "$ARCH"/20*/; do
    [[ -d "$d" ]] || continue
    if [[ "$(stat -c %G "$d")" != dikarya || ! -g "$d" ]]; then
        echo "   repairing ${d#$APP/}"
        run chgrp -R dikarya "$d"
        run chmod 2770 "$d"
        run find "$d" -type f -exec chmod g+rw {} +
    fi
done

# --------------------------------------------------------------------------
say "2. nginx buffers"
SITE=/etc/nginx/sites-available/dikarya
if cmp -s "$APP/ops/nginx/dikarya" "$SITE"; then
    # Reload anyway: a run that installed the file but failed to reload left
    # nginx on the old configuration, and a re-run must not skip that.
    echo "   already installed"
    if (( ! DRY )); then
        nginx -t && systemctl reload nginx && echo "   nginx reloaded"
    fi
else
    backup "$SITE"
    run install -o root -g root -m 0644 "$APP/ops/nginx/dikarya" "$SITE"
    if (( ! DRY )); then
        if nginx -t; then
            systemctl reload nginx
            echo "   nginx reloaded"
        else
            echo "   nginx -t FAILED; restoring the previous site file" >&2
            cp -a "$SITE.bak-$STAMP" "$SITE"
            nginx -t && systemctl reload nginx
            exit 1
        fi
    fi
fi

# --------------------------------------------------------------------------
say "3. redis authentication"

# Existing REDIS_URL in the service environment, if any (never sourced).
current_url=$(sed -n -E 's/^[[:space:]]*REDIS_URL=//p' "$ENV_FILE" | tail -1 | tr -d "\"'")
pw=""
if [[ -n "$current_url" ]]; then
    if [[ "$current_url" =~ ^redis://:([0-9a-f]{64})@(localhost|127\.0\.0\.1):6379/([0-9]+)$ ]]; then
        pw=${BASH_REMATCH[1]}; db=${BASH_REMATCH[3]}
        echo "   REDIS_URL already carries a password; reusing it"
    elif [[ "$current_url" =~ ^redis://(localhost|127\.0\.0\.1)(:6379)?/([0-9]+)$ ]]; then
        db=${BASH_REMATCH[3]}
    else
        echo "   REDIS_URL in $ENV_FILE has a shape this script does not know: refusing." >&2
        echo "   Set requirepass and REDIS_URL by hand." >&2
        exit 78
    fi
else
    db=0
fi
[[ -n "$pw" ]] || pw=$(openssl rand -hex 32)
url="redis://:${pw}@localhost:6379/${db}"

# The password never goes on a command line (any local user can read those):
# redis-cli takes it from REDISCLI_AUTH, and CONFIG SET goes in on stdin.
# Is Redis already enforcing this password?
if REDISCLI_AUTH="$pw" redis-cli --no-auth-warning -n "$db" ping 2>/dev/null | grep -q PONG \
   && ! redis-cli -n "$db" ping 2>/dev/null | grep -q PONG; then
    already_live=1
else
    already_live=0
fi

# Refuse while anything is queued or running: the restart below would kill it.
busy=0
# -n: the queues live in REDIS_URL's database, which need not be 0. Before
# the password is live, REDISCLI_AUTH must be unset, not empty.
rcli() {
    if (( already_live )); then
        REDISCLI_AUTH="$pw" redis-cli --no-auth-warning -n "$db" "$@"
    else
        redis-cli -n "$db" "$@"
    fi
}
for q in phylo_high phylo_bulk voucher_sync; do
    n=$(( $(rcli LLEN "rq:queue:$q") + $(rcli ZCARD "rq:wip:$q") ))
    echo "   $q queued+running=$n"
    (( n == 0 )) || busy=1
done
if (( busy )); then
    echo "   Jobs are in flight; nothing changed for step 3. Re-run when the queues are empty." >&2
    exit 75
fi

# Preflight BEFORE changing anything: a broken import must not be restarted
# into, and the password must not go live for processes that cannot start.
# As tree, not root: a root import would leave root-owned __pycache__ files.
if ! runuser -u tree -- "$APP/scripts/dikarya-preflight"; then
    echo "   preflight failed; nothing changed for step 3. Fix the import and re-run." >&2
    exit 1
fi

if (( DRY )); then
    echo "   [dry-run] would set requirepass, write REDIS_URL (db $db) to $ENV_FILE and $TREE_ENV, restart services"
    exit 0
fi

# 3a. Service environment first, so every process started from here on has it.
backup "$ENV_FILE"
tmp=$(mktemp "$ENV_FILE.XXXXXX")
grep -v -E '^[[:space:]]*REDIS_URL=' "$ENV_FILE" > "$tmp" || true
printf 'REDIS_URL=%s\n' "$url" >> "$tmp"
chown --reference="$ENV_FILE" "$tmp"; chmod --reference="$ENV_FILE" "$tmp"
mv "$tmp" "$ENV_FILE"

# 3b. Copy for the tree account (cron scripts, agents' redis-cli queue checks).
install -d -o tree -g tree -m 0700 /home/tree/.dikarya
umask 077
printf 'REDIS_URL=%s\nREDISCLI_AUTH=%s\n' "$url" "$pw" > "$TREE_ENV"
chown tree:tree "$TREE_ENV"; chmod 0600 "$TREE_ENV"

# 3c. Redis config (persists across restarts) -- the file now holds a secret.
backup "$REDIS_CONF"
# Rewritten with shell builtins only (read/printf), so $pw is never an
# argument of an external command such as sed.
tmp=$(mktemp "$REDIS_CONF.XXXXXX")
replaced=0
while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^[[:space:]]*requirepass[[:space:]] ]]; then
        printf 'requirepass %s\n' "$pw"; replaced=1
    else
        printf '%s\n' "$line"
    fi
done < "$REDIS_CONF" > "$tmp"
if (( ! replaced )); then
    printf '\n# Dikarya, %s: RQ unpickles queued jobs; never run without a password.\nrequirepass %s\n' "$STAMP" "$pw" >> "$tmp"
fi
chown root:redis "$tmp"; chmod 0640 "$tmp"
mv "$tmp" "$REDIS_CONF"

# 3d. Go live without restarting Redis, then restart the clients at once.
if (( ! already_live )); then
    printf 'CONFIG SET requirepass %s\n' "$pw" | redis-cli >/dev/null
fi
echo "   requirepass live"

set +e
/usr/local/sbin/restart-dikarya-web; web=$?
# The guarded wrappers, not a bare systemctl restart: they re-check the
# queues and each unit's graceful-shutdown drop-in at the moment of restart,
# so a job that arrived since the check above is drained, never killed. With
# no consent on stdin a busy queue answers 75 and nothing is restarted.
/usr/local/sbin/restart-dikarya-worker </dev/null; wrk=$?
/usr/local/sbin/restart-dikarya-worker-bulk </dev/null; bulk=$?
/usr/local/sbin/restart-dikarya-metrics; met=$?
set -e

# --------------------------------------------------------------------------
say "verification"
redis-cli ping 2>&1 | grep -q NOAUTH && echo "   ok: unauthenticated redis-cli is refused" \
    || echo "   WARNING: unauthenticated redis-cli was NOT refused" >&2
REDISCLI_AUTH="$pw" redis-cli --no-auth-warning ping | grep -q PONG && echo "   ok: password works"
# Bounded wait for every expected unit to be active and its RQ worker
# registered; "Done" only when both hold.
units=(dikarya-web dikarya-worker dikarya-worker-high2 dikarya-worker-voucher dikarya-worker-bulk dikarya-metrics)
for attempt in $(seq 1 30); do
    inactive=0
    for u in "${units[@]}"; do [[ "$(systemctl is-active "$u")" == active ]] || inactive=1; done
    registered=$(REDISCLI_AUTH="$pw" redis-cli --no-auth-warning -n "$db" SCARD rq:workers)
    (( inactive == 0 && registered >= 4 )) && break
    sleep 2
done
for u in "${units[@]}"; do
    printf '   %-24s %s\n' "$u" "$(systemctl is-active "$u")"
done
echo "   RQ workers registered: $registered (expect 4)"
echo "   web-restart=$web worker-restart=$wrk bulk-restart=$bulk metrics-restart=$met (0 = ok; web 69/70/75: see CLAUDE.md table; worker 75 = busy, re-run that wrapper)"
(( web == 0 && wrk == 0 && bulk == 0 && met == 0 && inactive == 0 && registered >= 4 )) || exit 1
echo
echo "Done. The tree account can now run:  set -a; . ~/.dikarya/redis.env; set +a; redis-cli LLEN rq:queue:phylo_high"
