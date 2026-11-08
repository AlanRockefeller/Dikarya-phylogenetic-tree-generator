The October 2026 log-review improvements add shared NCBI EUtils pacing,
resumable BLAST queries, background-operation correlation, import outcome
counts, browser asset summaries and disk-growth diagnostics. Web, worker and
metrics processes need their usual guarded restart after the preflight check.

The digest invokes the existing read-only nginx wrapper with `sudo -n` and
reports missing permissions or incomplete history as coverage warnings.
`--skip-nginx` disables this supplementary collection. It never changes the
log-review checkpoint unless `--mark-reviewed` is explicitly supplied.

For full nginx access history on this shared host, a host administrator must
install the updated fixed-path wrapper, which filters Dikarya traffic before
applying the line cap:

```sh
sudo install -o root -g root -m 0755 /var/www/dikarya/scripts/dikarya-nginx-log /usr/local/sbin/dikarya-nginx-log
```

Its new `app-access-all` and `app-error-all` keywords accept only the existing
bounded line-count argument. They introduce no caller-selected path, shell
fragment, hostname or journal unit. Existing sudoers permission for the wrapper
continues to apply. An older installation remains usable through a fallback,
but the digest reports that the access history can be incomplete.

MycoMap.org 401/403 refusals temporarily mark new searches unavailable across
web processes. Changing the configured key does not inherit the old refusal;
otherwise the circuit allows another attempt after ten minutes. Public result
imports are unaffected. This handles a refused grant; it cannot repair or
replace a grant controlled by MycoMap.org. The public availability endpoint
exposes only availability, a friendly message and retry delay.

BLAST continuation state contains only an upstream RID and timing, under the
existing query hash in the BLAST cache. It expires after four hours. Per-query
file locking prevents simultaneous duplicate submissions/polls. No RID is
accepted from a browser; another attempt with the same query/settings resumes
its saved search, and invalid or failed upstream RIDs are discarded.

EUtils slots and Retry-After cooldowns use a short-timeout Redis client with
a process-local fallback and a degradation warning. Every attempt is paced,
and waits and retries stay inside the caller's existing deadline. Test Redis
keys and response archives are isolated from production.

The November 6 health review adds five related fixes:

- The nginx reader accepts timing and correlation fields after the host/server
  fields. The digest accepts both formats, reports unparseable or stale access
  history, and supplements stale scoped history only when the bounded unfiltered
  reader recovers newer Dikarya records. Scoped records are retained, overlap is
  counted once, and unrelated hosts cannot make coverage appear current. Full
  history still requires installing the updated root-owned reader using the
  command above; nginx itself needs no restart.
- Submissions check each builder's minimum sequence count after offline dedup,
  before creating a database row. The browser uses the same limits: IQ-TREE and
  Quick Tree need three sequences; RAxML-NG and MrBayes need four; FastTree and
  neighbor joining accept two. Single-query auto-BLAST submissions and deferred
  imports are checked by the worker after expansion. Multi-record FASTA does not
  trigger auto-BLAST. The final post-processing guard remains in place.
- A resumable NCBI BLAST timeout returns HTTP 202 with a pending status when
  the client opts in with X-Dikarya-BLAST-Pending: 1. Existing open tabs and
  clients without that header retain their understood resumable 504 response. The
  background poller returns control, and all three browser BLAST entry points
  offer Continue NCBI BLAST while retaining the query. A timeout without a
  saved upstream search still reports an error. Request budgets are unchanged.
- An early-stopped MrBayes run that fails its final diagnostics can continue
  once from its checkpoint with the ASDSF stop rule disabled. The extension is
  bounded, never run on to the full generation ceiling (from a stop at 20,000
  that was ~50x the work): it ends at the earliest of 4x the first segment's
  generations, the length at which min ESS is predicted to reach 200 (x1.25),
  one hour (and the remaining MrBayes budget) at the first segment's measured
  speed, and the original ceiling. An extension under half the first segment
  is skipped. If the continuation fails for any reason, the first segment's
  finished tree and diagnostics are restored and returned (logged as
  `DEGRADED mrbayes_continuation_failed`) instead of failing the job.
  Only the final diagnostics produce a convergence warning. The metadata
  records the continuation (`from_generation`, `to_generation`, `limited_by`,
  `outcome`) and final stopping behavior. Runs without an early stop or with
  no readable checkpoint are not extended.
  This uses the documented checkpoint/append workflow in the
  [MrBayes manual, section 5.3.10](https://raw.githubusercontent.com/NBISweden/MrBayes/develop/doc/manual/Manual_MrBayes_v3.2.pdf).
- Worker health checks expected staffing per queue, including voucher_sync,
  and includes voucher backlog in queue health. A 120-second collector startup
  grace avoids warning before new workers have registered. Operators can set
  EXPECTED_PHYLO_HIGH_WORKERS (default 2), EXPECTED_PHYLO_BULK_WORKERS (1),
  EXPECTED_VOUCHER_SYNC_WORKERS (1), and WORKER_HEALTH_STARTUP_GRACE_SECONDS (120).

Validation on November 6: 662 relevant Python tests and 66 subtests passed.
The updated nginx parser accepted 250 captured production records; the old
host filter accepted none of those records.
The BLAST provider, background polling and sequence-count Node harnesses pass;
the full inline script passes syntax checks. ESLint reports the same four
pre-existing findings as HEAD, with no added findings. The preflight import
check passes. A real temporary-directory MrBayes run resumed at generation
20,000 and stopped at the original 100,000 limit; ESS rose from 9 to 41 and
the remaining low-ESS warning was correctly preserved. No saved job was rerun.

The web service and high-priority workers were restarted through their
permitted wrappers. The worker/voucher queues were checked idle first, and
new startup records for both high-priority workers and voucher_sync were
verified. The bulk and metrics restart commands are blocked by this session's
sandbox (sudo reports no-new-privileges); the installed nginx reader is also
outside the agent's permitted write scope.

To finish deployment from an administrator's host session:

1. Install the root-owned nginx reader using the command above.
2. Run scripts/dikarya-preflight.
3. Recheck redis-cli LLEN rq:queue:phylo_bulk and
   redis-cli ZCARD rq:wip:phylo_bulk. When both are zero, run
   sudo /usr/local/sbin/restart-dikarya-worker-bulk. If the guard reports active
   jobs (exit 75), wait for them; do not authorize interruption.
4. Run sudo /usr/local/sbin/restart-dikarya-metrics, then verify the bulk worker
   startup and the metrics journal/metrics-file timestamp. After the collector
   startup grace, each of the three queue staffing checks should be healthy.

What's New draft (not published):

Title: More reliable tree submissions and searches
Category: improvement
Body: Tree Builder checks sequence counts before queueing and shows pending
NCBI BLAST searches with a Continue action. MrBayes can continue an early-stopped
run from its checkpoint when sampling is insufficient, within the original
limits. Worker and proxy-log monitoring now detects missing queue workers and
log coverage gaps.
