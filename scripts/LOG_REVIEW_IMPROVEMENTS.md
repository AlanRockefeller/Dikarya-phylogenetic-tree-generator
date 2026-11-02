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
