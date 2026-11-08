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


Deployment status, checked November 8: the nginx reader was installed on
November 6 (16:59 UTC) and matches the repository copy; the bulk worker restarted
at 18:13 that day; the metrics collector was still the boot-time process from
09:10, before the staffing checks, and was restarted on November 8 at 07:01 UTC.
Inside the agent sandbox the digest still reports the nginx reader as
"unavailable (wrapper exit 1)": that is sudo refusing under no-new-privileges,
not a broken reader. Run the digest outside the sandbox for nginx coverage.

The November 8 review (window 2026-11-04T05:22:54Z to 2026-11-08T06:33:51Z)
found no attack aimed at Dikarya. Its fixes:

- The path crowd's expiry slides with each observation instead of being set
  once at key creation. The old rule wiped each path's history seven days after
  it was first seen, so a .env sweep landing after a reset scored its app-shaped
  paths as never-seen singletons and escalated (35.189.61.0/24). A replay of all
  70 requests for /core/app/.env since September 1 reproduced the reset exactly.
- A scanner word used as a job id (/job/.env) is scanner noise, not
  job_id_malformed, and traversal counts as app-surface only when it starts
  inside one of our routes (/static/../, not the stock /static../ nginx probe).
- The digest drops the http.request_failed row of a 4xx the access log marked
  noise=scanner, so scanner requests on real routes are not listed twice, and
  has a New accounts section that flags dotted Gmail addresses. Registrations
  were INFO-only, so an automated signup went unnoticed for several days until
  this review; that account has been disabled.
- A recompute whose root clade no longer exists (pruned away, or no longer
  grouped together) falls back to midpoint rooting with a notice, like a lost
  root tip, instead of keeping the builder's arbitrary root (job jtk5). The
  log line names which way the clade was lost.
- Prune, rotate and reroot refusals carry reason codes (prune_would_empty_tree,
  prune_targets_not_found, reroot_target_not_found, ...) instead of bad_request.
- raxml_parameters_adjusted is no longer DEGRADED for the publication-preset
  early-stopping override, which makes the search more thorough; it was the
  only thing that had ever triggered it.
- /job/<id>/ redirects to /job/<id>; the decoy id keeps answering 404.
- nginx: ops/nginx/00-bot-blocklist (new repository copy) lets blocked crawlers
  read /robots.txt, which already told most of them "Disallow: /"; robots.txt
  gains groups for BLEXBot, ZoominfoBot and serpstatbot. Installed 2026-11-08
  09:14 UTC; verified that SemrushBot gets robots.txt (200) and a 403 elsewhere.
- The /amanitaceae/ mirror (not in git; backup of every HTML and CSS file at
  ~/.dikarya/backups/amanitaceae-html-css-2026-11-08.tar.gz): its jQuery UI
  imports, theme images (restored from jQuery UI 1.8.24, byte-identical to the
  copies the mirror already had) and font are fixed; 1,466 page backgrounds and
  3,316 magnifier images point at their archived copies; 2,091 links to pages
  that were never archived point at unavailable.html. An inert snippet in 1,465
  pages that only removed a BeEF hook <script> (left by the original site's
  admins after a compromise; no such tag exists in the mirror) was deleted.

Follow-up the same day -- what happened to the mirror's original site, and how
the mirror is now secured:

- The compromise was on the old host, not here. Every mirror file still had its
  2026-05-30 build time when the cleanup started (the one later file is git
  tracked and clean), and Wayback captures of amanitaceae.org show the
  injection there: clean through 2023-07; two live
  <script src="https://beef.beonlineboo.com/hook.js"> tags in CMS content
  snippets in 2024-02 and 2024-05; tags gone by 2024-08, leaving the site's own
  removal snippet; by 2024-12 that snippet targeted beef.softbyms.com; the site
  stopped answering by 2025-02 and still does not.
- The script (archived copies read statically, never run) reported every page
  view to the attacker's server and, for visitors the server chose, covered the
  page with a fake "install the browser extension to continue" prompt (styled
  after 1C:Enterprise's extension prompt) whose Download button fetched a file
  chosen by the server. Group-IB tracks the operator as "Boolka": opportunistic
  SQL injection into websites since 2022, and a BeEF-based delivery platform
  for BMANAGER, a Windows downloader with keylogger and file/wallet-stealer
  modules that installs to C:\Program Files\Full Browser Manager\.
- The mirror never carried a live hook, and no mirrored JavaScript was altered
  (jQuery 1.7.2 is byte-identical to the official build; no library carries
  appended code). The last inert snippet (beonlineboo, one page) is removed,
  and so is Google Analytics: every page loaded ga.js from Google into this
  origin and reported visits to the original site's UA-18752463-1.
- ops/nginx/dikarya (repository copy of the site config) adds a CSP sandbox,
  nosniff, no-referrer and HSTS to /amanitaceae/; tested in a throwaway nginx,
  installed 2026-11-08 09:14 UTC (pre-install copies of both files are beside
  them as *.bak-20261108) and verified live: mirror responses carry the
  sandbox policy and the main app's do not. A second HTML backup
  taken before the analytics removal is
  ~/.dikarya/backups/amanitaceae-html-2026-11-08-before-ga-removal.tar.gz.
