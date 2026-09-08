# Guarded worker restarts

## Restart the bulk worker after its current job

After the one-time installation below, run:

```sh
sudo /usr/local/sbin/restart-dikarya-worker-bulk-when-idle
```

This finishes the active job, restarts the worker, then continues queued jobs
with the updated code. It does not wait for the entire queue to empty. An idle
worker restarts immediately. The command returns once systemd accepts the
request, and you can close the terminal while systemd waits for the job.
There is no force mode or interruption prompt, and no timeout that kills the
current job. A stuck job can therefore delay the restart.

The command validates the effective graceful-shutdown settings and runs
preflight as `dikarya` before scheduling. It serializes requests using a
root-owned lock and leaves an already pending service operation alone rather
than sending RQ a second shutdown signal. A service stop requested by another
operator is not converted into a restart; check its status before retrying.

Exit 0 means the restart was scheduled **or a service operation was already
pending**, not that the new worker is ready. Exit 75 means another invocation
is checking the request; retry after it returns. Exit 78 means configuration,
preflight, or another check failed; exit 70 means systemd rejected the request.
If a command times out, inspect service status before retrying because systemd
may already have accepted it.

Verify completion with the status command printed by the wrapper and the log:

```sh
systemctl show dikarya-worker-bulk.service -p ActiveState -p SubState -p MainPID
tail -n 30 /var/www/dikarya/var/logs/worker-bulk.log
```

Expect a different MainPID, `active`/`running`, and the new worker's queue startup
message. During the wait the service may show `deactivating`; the current job
continues running. The command prints the previous MainPID for comparison.

### One-time installation (administrator/root shell)

These commands install the helper and its narrow sudo grant. They do **not**
schedule a restart or interrupt a running job. The high-worker wrapper is
unchanged. The existing immediate bulk-restart command remains available.

```sh
install -d -o root -g root -m 0755 /usr/local/libexec /etc/systemd/system/dikarya-worker-bulk.service.d
install -o root -g root -m 0755 /var/www/dikarya/scripts/restart-dikarya-worker /usr/local/libexec/dikarya-worker-restart
install -o root -g root -m 0755 /var/www/dikarya/scripts/restart-dikarya-worker-bulk /usr/local/sbin/restart-dikarya-worker-bulk
install -o root -g root -m 0755 /var/www/dikarya/scripts/restart-dikarya-worker-bulk-when-idle /usr/local/sbin/restart-dikarya-worker-bulk-when-idle
install -o root -g root -m 0644 /var/www/dikarya/scripts/dikarya-worker-graceful.conf /etc/systemd/system/dikarya-worker-bulk.service.d/zz-graceful.conf
visudo -cf /var/www/dikarya/scripts/dikarya-worker-bulk.sudoers && install -o root -g root -m 0440 /var/www/dikarya/scripts/dikarya-worker-bulk.sudoers /etc/sudoers.d/dikarya-worker-bulk
systemctl daemon-reload
systemctl show dikarya-worker-bulk.service -p KillMode -p KillSignal -p TimeoutStopUSec -p ExecStop
```

Expected settings: `mixed`, SIGTERM (`15`), `infinity`, and no ExecStop command.
Then run the new command when you want to schedule the restart.

## Install only the bulk-worker guard

Run as root. This leaves the interactive-worker wrapper unchanged:

```sh
install -d -o root -g root -m 0755 /usr/local/libexec /etc/systemd/system/dikarya-worker-bulk.service.d
install -o root -g root -m 0755 /var/www/dikarya/scripts/restart-dikarya-worker /usr/local/libexec/dikarya-worker-restart
install -o root -g root -m 0755 /var/www/dikarya/scripts/restart-dikarya-worker-bulk /usr/local/sbin/restart-dikarya-worker-bulk
install -o root -g root -m 0644 /var/www/dikarya/scripts/dikarya-worker-graceful.conf /etc/systemd/system/dikarya-worker-bulk.service.d/zz-graceful.conf
visudo -cf /var/www/dikarya/scripts/dikarya-worker-bulk.sudoers && install -o root -g root -m 0440 /var/www/dikarya/scripts/dikarya-worker-bulk.sudoers /etc/sudoers.d/dikarya-worker-bulk
systemctl daemon-reload
systemctl show dikarya-worker-bulk -p KillMode -p TimeoutStopUSec
```

Expected: `KillMode=mixed` and `TimeoutStopUSec=infinity`. Then run:

```sh
/usr/local/sbin/restart-dikarya-worker-bulk
systemctl show dikarya-worker-bulk -p ActiveState -p SubState -p MainPID
tail -n 15 /var/www/dikarya/var/logs/worker-bulk.log
```

The guard reports active jobs before offering interruption. For agents, the
command is `sudo /usr/local/sbin/restart-dikarya-worker-bulk`; exit 75 means ask
the user rather than automatically confirming. The shared root implementation
is loaded only from `/usr/local/libexec`, never from the writable repository.

## Install both workers using the original shared entry point

Install as root (no running job is interrupted by installation or daemon-reload):

```sh
sudo install -o root -g root -m 0755 /var/www/dikarya/scripts/restart-dikarya-worker /usr/local/sbin/restart-dikarya-worker
sudo install -o root -g root -m 0755 /var/www/dikarya/scripts/restart-dikarya-worker /usr/local/sbin/restart-dikarya-worker-bulk
sudo install -o root -g root -m 0644 /var/www/dikarya/scripts/dikarya-worker-graceful.conf /etc/systemd/system/dikarya-worker.service.d/zz-graceful.conf
sudo install -o root -g root -m 0644 /var/www/dikarya/scripts/dikarya-worker-graceful.conf /etc/systemd/system/dikarya-worker-bulk.service.d/zz-graceful.conf
sudo systemctl daemon-reload
```

The existing high-worker sudo grant stays unchanged. To let agents operate the
bulk worker, add the equally scoped `/usr/local/sbin/restart-dikarya-worker-bulk`
with no arguments to the existing sudoers policy using visudo. Do not grant
arbitrary systemctl access.

Run the usual wrapper. It reports queue length, active job IDs, account email
(or anonymous), input summary, elapsed seconds and remaining timeout budget.
The timeout is an upper bound, **not a predicted finish time**. We do not yet
have a reliable runtime estimator for arbitrary sequence inputs.

Idle workers restart without a question. If a job arrives during that check,
graceful shutdown drains it safely; the restart command returns immediately and
systemd finishes the restart when the job ends.

Busy workers remain untouched by default. Interactive users may type the exact
`INTERRUPT <job-ids>` line to discard the listed jobs' current work. Agents get
JSON plus exit 75; they must show the report and ask the user whether to wait
or interrupt. A pending queue alone is safe: it is stored in Redis.

After explicit approval, send that exact printed line to the same wrapper on
stdin. Approval applies only to those job IDs. The wrapper freezes the worker
briefly and checks the IDs again before killing; if they changed it thaws and
refuses. Never fabricate consent, use a blanket force flag, or keep polling a
running job without the user's choice to wait.

Exit 0: restart requested, verify ActiveState/MainPID and worker log.
Exit 75: human decision required or approved job set changed.
Exit 78: safety check/configuration failed, no restart authorized.

The root wrapper uses only Python's standard library. Database/Redis inspection
runs as dikarya with only the necessary environment settings, never as root;
it does not deserialize RQ pickle data. Keep the installed wrapper root-owned.
The Redis and database connection must be available or the restart is refused.

Unlimited graceful drain also applies to ordinary system shutdown. A stuck
analysis can delay it until the pipeline timeout, so an operator may still
need an explicitly approved immediate stop. This does not preserve a job
through power loss, OOM, or a host reboot forced before draining finishes.
