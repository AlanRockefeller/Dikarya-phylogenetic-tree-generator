# AGENTS.md

This file provides guidance to coding agents, including Claude Code (claude.ai/code),
when working with code in this repository. `CLAUDE.md` is a local symlink to this file.

## What This Is

Dikarya is a Flask web application for fungal phylogenetic analysis. Users submit DNA sequences (FASTA or GenBank accessions), which are run through a configurable bioinformatics pipeline (alignment → trimming → tree building) via background workers, with real-time status updates via Server-Sent Events. Results are displayed in an interactive tree viewer.

## Commands

```bash
# Activate the virtual environment first
source .venv/bin/activate

# Tests: always invoke pytest through the project interpreter. Calling
# .venv/bin/pytest directly can omit the repository root from sys.path and make
# collection fail with "ModuleNotFoundError: No module named 'app'".
.venv/bin/python -m pytest

# Database migrations
flask db migrate -m "description"
flask db upgrade

# Run web app (dev)
export FLASK_APP=wsgi.py
export FLASK_ENV=development
flask run --port=5000

# Start background worker (required for job processing)
flask run-worker

# Start metrics collector
flask run-metrics

# Check whether any job is running BEFORE restarting the worker (which kills them).
# Exits 1 and lists the jobs if any are in flight.
flask jobs-in-flight

# Reconcile Postgres against RQ, then mark long-abandoned jobs as failed. Each
# stuck job keeps an SSE stream alive, and every open stream holds one of the
# (workers x threads) request slots, so leaving them can make the site stop
# responding. Always dry-run first. Connects to production PostgreSQL
# (escalated/outside-sandbox).
flask reap-stuck-jobs --older-than-days 1 --dry-run
flask reap-stuck-jobs --older-than-days 1

# After explicit user approval only: add a What's New changelog entry.
# This connects to production PostgreSQL, so run it with escalated/outside-sandbox
# permissions on the first attempt rather than trying it in the sandbox first.
source .venv/bin/activate && scripts/dikarya_whats_new.py add --title "Title here" --body "Description of what changed." --category feature
# Categories: feature, fix, improvement, update

# List existing What's New entries (also requires production PostgreSQL access,
# so use escalated/outside-sandbox permissions on the first attempt)
source .venv/bin/activate && scripts/dikarya_whats_new.py list
```

Production runs under systemd as `dikarya-web.service` (Gunicorn) and `dikarya-worker.service` (RQ worker). Agents can restart these services via the sudo wrappers documented in the "Restarting Dikarya services" section below — do not ask for permission, just run the appropriate wrapper after changes that require it. **Restarting the web service is safe; restarting the worker kills any job that is currently running — check first (see "Restarting Dikarya services").**

## Architecture

### Pipeline Steps (in order)

`INPUT → ORIENT → BLAST → ALIGN → TRIM → TREE → POST`

ORIENT (sequence orientation), BLAST (homolog search), and TRIM are optional. The tree viewer at `/job/<id>/view` allows post-hoc pruning, rerooting, and renaming.

### Services (`app/services/`)

Each bioinformatics step has its own service module. All external tool invocations go through `subprocess_utils.py`. Input validation and path safety live in `security_utils.py`. The `access_control.py` module enforces job ownership.

### Job Parameters

`app/models.py` defines the dataclasses: `JobParams`, `AlignmentParams`, `TreeBuilderParams`, `TrimmingParams`. These are serialized to `var/jobs/{job_id}/input_info.json`.

### File Layout on Disk

Verified against all ~10,900 job directories. The previous version of this block
listed `aligned.fasta`, `trimmed.fasta`, `tree/tree.nexus` and a root-level
`blast_results.json`, none of which the pipeline has ever written — the v1 API's
artifact map had copied the same wrong paths and those downloads always 404'd.

```
var/jobs/{job_id}/
  input_info.json          submitted params + the ORIGINAL submitted FASTA
  tree_state.json          viewer state: pruning, renames, rooting
  input/input_raw.fasta    the PROCESSED input (deduped/oriented/BLAST-augmented)
  blast/blast_results.json
  alignment/alignment_raw.fasta          aligned, untrimmed
            alignment_trimmed.fasta      what the tree builder consumed
            alignment_trimmed_report.html   trimAl -htmlout (gzipped at rest)
            alignment_pruned*.fasta      recompute's derived set
  tree/tree_original.{newick,nexus}
       tree_pruned.{newick,nexus}
       tree_metadata.json
  logs/{pipeline,alignment,tree_builder}.log
```

**`input_info.json["sequence"]` is not a duplicate of `input/input_raw.fasta`.**
The first is exactly what the user submitted; the second is what the pipeline
derived from it after dedup, orientation and BLAST augmentation. They differ in
43% of jobs (records both added and removed). Recompute and the
restore-removed-duplicates endpoint both need the original, so do not "dedupe"
these against each other.

### Compressed artifacts

Large cold artifacts are stored gzipped. **Never `open()` a job artifact
directly** — go through `app/services/artifact_storage.py`, which resolves
`foo.fasta` to `foo.fasta.gz` transparently:

| Instead of | Use |
|---|---|
| `open(p)` | `open_artifact(p, "rt")` |
| `p.is_file()` | `artifact_exists(p)` |
| `p.stat().st_size` | `artifact_size(p)` (uncompressed size) |
| `p.read_bytes()` | `read_artifact_bytes(p)` |
| `send_file(p)` | resolve first; send decompressed bytes for a `.gz` |

The plain file always wins when both forms exist, so a step that rewrites an
artifact just writes plain as before — call `discard_gzipped_form(p)` first so
the stale archive does not linger. Currently gzipped: the trimAl HTML reports
and the aligned/trimmed FASTAs. Deliberately left plain: `input_raw.fasta`,
`tree_state.json` and `input_info.json`, because each is rewritten in place by a
normal user action (add sequences, every viewer edit, recompute).

`scripts/dikarya_reclaim_job_space.py` applies all of this retroactively and
runs weekly from `ops/cron/dikarya-reclaim-job-space`:

```bash
.venv/bin/python scripts/dikarya_reclaim_job_space.py --dry-run   # always first
.venv/bin/python scripts/dikarya_reclaim_job_space.py --apply
```

Jobs touched within `--min-age-hours` (default 24) are skipped, so a live run is
never disturbed. The passes (`scratch`, `logs`, `json`, `reports`, `alignments`)
are independent and individually selectable with `--passes`.

### Writing to var/jobs

`var/jobs` is `dikarya`-owned but **group-writable**, and the `tree` account is
in the `dikarya` group, so agents can run maintenance scripts against it
directly. Two things make that hold:

- Directories carry setgid (`chmod g+ws`), so a new job directory inherits group
  `dikarya` rather than the creating process's primary group.
- `dikarya-web`, `dikarya-worker` and `dikarya-metrics` run with `UMask=0002`
  via `/etc/systemd/system/<unit>.service.d/umask.conf` (source:
  `scripts/dikarya-umask.conf`), so the files they create are 0664 / 2775
  instead of 0644 / 0755. Without it a chmod pass would be undone by the next
  thing the pipeline wrote.

**A session that predates the group grant will still be denied.** Supplementary
groups are stamped at login, so an agent started from an older shell inherits
the old set — restarting the CLI inside that shell changes nothing, it takes a
fresh login. Check with `id` before concluding the permissions are wrong.

Code that writes a job artifact atomically (mkstemp → `os.replace`) must set the
mode explicitly, because mkstemp creates 0600. Preserve the target's existing
mode when it has one and fall back to `default_file_mode()` from
`app/services/artifact_storage.py` when it does not — never a hardcoded 0644.
`tree_state.json` is the cautionary tale: its creation path hardcoded 0644 and
every later save *preserved* that, so the one file every viewer edit rewrites
stayed group-unwritable in a job directory where everything else was 0664.

## Restarting Dikarya services

### Guarded worker wrapper (installation required)

`scripts/WORKER_RESTART.md` documents the new root-owned replacement wrapper
and graceful-shutdown drop-in. Until those are installed, the legacy safety
checks below still apply. After installation, use the same high-worker wrapper;
the separately granted `restart-dikarya-worker-bulk` handles bulk jobs.

The guarded wrapper prints JSON with owners, job details, elapsed time and a
timeout budget (not an ETA). Exit 75 means **show the report to the user and ask
whether to interrupt or wait**. Do not automatically confirm. Only after the
user explicitly approves losing those jobs' current work, send the exact
printed `INTERRUPT <job-ids>` line on stdin. Changed IDs require a fresh choice.
Exit 78 means a failed safety/configuration check; do not bypass it. Exit 0
means restart requested, not necessarily finished: verify worker state/logs.
An idle-check race drains the newly started job safely in the background.

For a requested restart **after the current bulk job**, use
`sudo /usr/local/sbin/restart-dikarya-worker-bulk-when-idle` once installed
(see `scripts/WORKER_RESTART.md`). It schedules a graceful systemd restart,
returns immediately, and never authorizes interrupting work. Queued jobs resume
after restart. Its exit 75 means another scheduling request is being checked,
not a request for interruption approval. Exit 0 means scheduled or an existing
service operation is pending; verify the new MainPID and worker startup logs.
Do not repeatedly signal a draining RQ worker: its second SIGTERM forces exit.

Agents may restart the Dikarya systemd services when needed after making changes. The `tree` user has limited passwordless sudo access to three root-owned wrapper scripts only:

```bash
sudo /usr/local/sbin/restart-dikarya-web
sudo /usr/local/sbin/restart-dikarya-worker
sudo /usr/local/sbin/restart-dikarya-metrics
```

Use these wrappers instead of running sudo systemctl restart ... directly.

The most commonly needed restart is:

sudo /usr/local/sbin/restart-dikarya-web

Run this after changes that affect the web application runtime, routes, views/templates, Python application code, configuration read by the web process, dependencies, or other behavior served by the Dikarya web app.

Use the worker or metrics restart wrappers only when the change affects those specific background services.

### Always check the restart wrapper's exit code

`restart-dikarya-web` does not just restart the unit — it then polls
`http://127.0.0.1:8000/health` until the app actually serves, and **exits
non-zero if it does not**. That check exists because `systemctl restart` returns
when *systemd* is satisfied, which is not the same as Dikarya being able to
answer a request: Gunicorn does not import the application until traffic
arrives, so a module that fails to import survives a "successful" restart (this
is the 2026-08-14 incident described below). The wrapper's curl forces that
first import while you are still watching.

A non-zero exit means **the site may be down right now**. Do not move on, and do
not simply re-run it:

| Exit | Meaning | What to do |
|---|---|---|
| 0 | Restarted and healthy | Continue. |
| 64 | Arguments were passed | The wrapper takes none, by design. |
| 69 | Restarted but never became healthy | **The site is down.** Read the journal lines the wrapper printed, then `sudo /usr/local/sbin/dikarya-journal web 200`. Usually an import error or a bad config value — fix it and restart again. |
| 70 | `systemctl restart` itself failed | systemd refused or failed the restart; read the `systemctl status` lines the wrapper printed, then `sudo /usr/local/sbin/dikarya-journal web 200`. (A missing `sudo` is exit 77, not this.) |
| 75 | Serving, but `/health` reports 503 | The restart worked and the code imported; a dependency (database, filesystem) is unhealthy. Restarting again will not fix it. |
| 77 | Not run as root | Re-run as `sudo /usr/local/sbin/restart-dikarya-web`. |

Do not mask the exit code — no `|| true`, and if you pipe the output, keep
stderr and check `${PIPESTATUS[0]}`.


### Run the preflight import check before any restart

```bash
scripts/dikarya-preflight && sudo /usr/local/sbin/restart-dikarya-web
```

It imports the modules the web and worker processes need and exits 1 with the
traceback if any of them fails. Gunicorn does not import worker code until a
request arrives, so an import error survives a "successful" restart and only
appears when a user hits it.

This is not hypothetical: on 2026-08-14 `app/workers/tasks.py` was deployed
importing a name `app/services/log_context.py` did not define yet. Because
`enqueue_job()` imports the task module inside the request handler, `POST
/api/job` returned 500 for 3.5 hours and seven queued jobs died in the worker
with RQ's misleading `ValueError: Invalid attribute name: run_phylo_job` — which
is what `import_attribute()` reports when the module behind the name will not
import. The preflight catches exactly this, and costs about a second.

Add new must-import modules to the list inside the script rather than replacing
it. The script deliberately does not call `create_app()`, which needs the
root-only `SECRET_KEY`, so it runs fine as `tree`.

### Restarting the worker kills running jobs

`restart-dikarya-worker` SIGKILLs the RQ work horse. Any job mid-run loses its work
— a user's alignment or tree simply dies partway through, and the killed process
never reaches the `except` block that would mark it failed, so its database row is
left stranded. This has really happened: on 2026-07-28 two agent-initiated worker
restarts killed two jobs mid-MAFFT (0-byte `alignment_raw.fasta`), and they sat at
`running` for nine days.

**Always check before restarting the worker:**

```bash
flask jobs-in-flight    # exits 1 and lists them if any job is live
```

**Agents usually cannot run that command.** It calls `create_app()`, which
requires `SECRET_KEY` from `/etc/dikarya/dikarya.environment.live` — a root-only
file. Use the equivalent Redis check instead; all four must print `0` before you
restart the worker:

```bash
redis-cli LLEN rq:queue:phylo_high  ; redis-cli ZCARD rq:wip:phylo_high
redis-cli LLEN rq:queue:phylo_bulk  ; redis-cli ZCARD rq:wip:phylo_bulk
```

`rq:queue:<name>` is the pending queue and `rq:wip:<name>` is RQ's
StartedJobRegistry, so a non-zero `wip` means a job is executing *right now* and
restarting will kill it. Re-run the check immediately before the restart, not
once at the start of a long task — a job can arrive in between.

If anything is in flight, wait for it to finish unless the user has accepted
losing that work. The web wrapper needs no such check — restarting Gunicorn only
drops in-progress HTTP requests (and briefly returns 502).

As a backstop, the worker reconciles Postgres against RQ on startup
(`app/services/job_reconcile_service.py`), so a job killed this way is marked
failed on the next boot rather than stranded forever. That limits the damage; it
does not give the user their tree back.

Do not attempt to use sudo for anything else. The tree user is intentionally restricted and should not be able to open a root shell, run arbitrary systemctl commands, edit sudoers, or restart unrelated services.

## What's New / Changelog System

The "What's New" page at `/whats-new` shows changelog entries stored in PostgreSQL, linked in the header nav between "Journal Home" and "Tree Builder".

**When you (an agent) make a major user-visible change, draft a What's New entry but do not add it yet.** Do not add an entry for minor changes. Draft the title, body, and category, then ask the user exactly: **"Add this to What's New?"** Only after explicit approval, add it — see the `whats-new` skill for the exact command, categories, and troubleshooting.

If the user directly asks to "add," "update," "publish," or "announce" something in What's New, that request itself is explicit approval. Do not ask for a second confirmation; choose the appropriate title, body, and category, then publish the entry.

The add and list commands connect to the production PostgreSQL database and require escalated/outside-sandbox permissions on the first attempt — see the `whats-new` skill for the exact invocation and verification steps.

## Claude Review ("Analyze with Claude")

The tree viewer's **Analyze with Claude** button posts to
`/api/job/<id>/analysis/review`, backed by
`app/services/tree_analysis_service.py`. Design details are in
`ARCHITECTURE.md`; the rules that matter when editing it:

- **Never send the alignment itself to the API.** Every statistic is computed in
  `summarize_alignment()` / `summarize_tree()` and only that summary is sent.
  This is what keeps the call fast and the numbers correct — do not "simplify"
  it by pasting FASTA into the prompt.

  The one sanctioned exception is `build_alignment_excerpt()`: at most two
  windows of at most `EXCERPT_MAX_COLUMNS` columns, cut around the largest
  interior gap of the most internally gapped rows, with a few clean neighbours
  beside them. It exists because no statistic separates a real indel from a row
  that has slipped out of register, and the fix differs. It is bounded on
  purpose and the prompt forbids counting anything from it — widening it into
  "just send the whole alignment" is the change this rule is here to prevent.
- **The review sees more than statistics now, and each extra block has a rule
  attached.** `tree.clade_structure` is the only thing that says which tips
  group together (strongly supported clades, outermost first, with a
  shape-only fallback that must never be described as supported), and
  `provenance` carries each sequence's origin from
  `input_info.json["sequence_metadata"]` — including the `identity` of the
  search that retrieved a reference, which is **not** a distance on this
  alignment. Taxon labels there are unverified: the reviewer may report that
  the labels and the tree disagree, never resolve which is right. If you change
  what these blocks contain, change the matching section of `SYSTEM_PROMPT`
  in the same edit.
- **Support classification must stay in step with the viewer.**
  `_classify_support()` mirrors `window.classifySupportType()` in
  `tree_viewer_phylotree_v2.js`. Change one and you must change the other, or
  the review and the on-screen support badge will disagree about the same tree.
  Both are driven by the tree-building method first and by the value shape only
  for an unrecognised method, both take the same `alrt_only` flag (IQ-TREE run
  with `-alrt` and no `-B` writes single SH-aLRT values, not UFBoot ones), and
  both are run over `tests/fixtures/support_classification_cases.json` by
  `tests/test_tree_analysis_metrics.py` — add a case there. `iqtree_fast` is
  declared `ALRT` outright rather than relying on that flag, because it can
  never produce a UFBoot value. The builder itself
  is resolved once, by `resolve_tree_support_context()`, which is what fills
  `window.TREE_METHOD`; do not resolve it separately in the template.
- **The `pipeline` block reports what the builder DID, not what was asked.**
  Four fields are deliberately not bare numbers, and each has a matching
  paragraph under "WHAT THE RUN ACTUALLY DID" in `SYSTEM_PROMPT`:
  `substitution_model` is the model that was *fit* (`model_selected`), with the
  request published beside it as `substitution_model_requested` only when the
  two differ — citing the request reported "MFP", which names no model, and
  called a GTR+G job GTR+G when it ran as GTR+F+G4. `tree_search` plus
  `tree_search_note` declare a deliberately limited search; the note is written
  self-contained so it still steers a review when the installed prompt copy is
  older than the field. `bootstrap_replicates` is a sentence whenever the
  builder did not run the requested count. For RAxML-NG, it reports the exact
  number of replicate trees written and whether AutoMRE declared convergence;
  old jobs recover both from `raxml_run.raxml.bootstraps` and `.log`.
  `bootstrap_metrics` names every metric computed, but only the
  first one's values are in the context, so the transfer-bootstrap numbers must
  never be quoted.
- **Bump `REVIEW_SCHEMA_VERSION`** whenever the prompt or the metric set changes
  in a way that makes an already-stored review misleading. Cached reviews at a
  different version are ignored rather than shown.
- **`scripts/dikarya-claude-review` must never forward arguments to `claude`.**
  It is invoked by a process handling untrusted internet input; `"$@"` there
  means `--tools Bash` and arbitrary code execution as `tree`. Every flag is
  pinned in the wrapper, the prompt comes in on stdin, and the sudoers entry
  ends in `""` so sudo permits no arguments either. If you need a new knob, add
  an environment variable with an allowlist check — do not add a parameter.
- **Sampled column metrics must never be published under an exact-count name.**
  When `column_metrics_are_estimates` is true the bare counts are omitted and
  only `*_estimated` fields are sent. Do not "restore" the plain names for
  convenience — the whole point is that a name without the suffix is always an
  exact tally.
- **Rooting comes from `tree_state.json`, never from topology.** A root of
  degree 3 and a null `outgroup` are both perfectly normal on a midpoint-rooted
  tree, which is the Dikarya default. If the state cannot be read, report the
  rooting as unknown.
- **Re-export the prompt files after editing `SYSTEM_PROMPT` or
  `RESPONSE_SCHEMA`.** The wrapper reads them from `/etc/dikarya-claude-review/`
  (root-owned, so the web process cannot rewrite the reviewer's instructions),
  which means they are a copy that can drift. `scripts/dikarya_export_review_prompt.py
  --check` fails on drift; re-run the two export commands in `ARCHITECTURE.md`.
- The default backend is `cli` (no API key; needs the root-owned wrapper
  installed). `CLAUDE_REVIEW_BACKEND=api` uses `ANTHROPIC_API_KEY` instead —
  put that in `/etc/dikarya/dikarya.environment.live`, **not** in a `.env` at
  the repo root, which crashes Gunicorn.
- The call is synchronous and holds a Gunicorn request slot for ~90–100s (a
  measured 119-tip review took 96s), inside
  nginx's `proxy_read_timeout 300`. The timeout chain (wrapper 240s < subprocess
  260s < nginx 300s) and the Redis concurrency ceiling are load-bearing. Do not
  remove them or raise the timeout past nginx.

## Key Conventions & UI Patterns

### Updating the browser iNaturalist Observation Finder

The browser port is rendered by `app/templates/inat_finder.html`; its vanilla
JavaScript lives in `app/static/js/inat_finder.js`. The upstream CLI is
`https://github.com/AlanRockefeller/inat.finder.py` and evolves independently.
When a new CLI version is published:

1. Compare the released CLI source, changelog, and tests with the browser port.
   Port the behavior intentionally rather than copying Python request/UI code.
   **The upstream default branch is `Main`, with a capital M** — a `raw.
   githubusercontent.com` fetch from `main` or `master` 404s. The vendored
   `inat_finder.py` is 1.8.1 and byte-identical to `Main`.
2. Preserve the browser security boundary: requests go directly from the browser
   to `https://api.inaturalist.org/v1`; Flask must not proxy or store searches.
3. Keep API-derived DOM content on `textContent`/`createElement` paths, and retain
   cancellation, request timeouts, retry delays, progress, dark mode, and mobile
   behavior when changing the search flow.
4. Update the version credited in the page footer only after the matching browser
   behavior has been implemented and checked. Update the focused Node coverage in
   `tests/js/inat_finder_variation_limit.test.js` for changed parsing, matching,
   variation, or request behavior, and `tests/js/inat_finder_auto_mode.test.js`
   for anything in the auto-mode ladder.
5. **The auto-mode candidate ladder is pinned to the CLI by fixture.**
   `tests/fixtures/inat_finder_candidate_parity.json` carries each stage's totals,
   label and a SHA-256 of its *ordered* candidate sequence, generated from the
   vendored `inat_finder.py` by `scripts/dikarya_export_finder_parity.py`. Order is
   load-bearing, not incidental: a paused deep search continues from where it
   stopped, so a JS plan that yields the same set in a different order silently
   skips or repeats candidates. After syncing `inat_finder.py`, re-run the export
   and expect `app/static/js/inat_finder.js` to change in the same commit if the
   hashes move — `tests/test_inat_finder.py` fails if the fixture and the vendored
   CLI disagree.
6. The browser sections the Node harnesses slice are delimited by
   `// ---- section:<name> ----` banners in `inat_finder.js`. Renaming one breaks
   the harness with "Finder section was not found", so move the banner with the
   code rather than deleting it.
7. Run `.venv/bin/python -m pytest tests/test_inat_finder.py`, run
   `node --check app/static/js/inat_finder.js` and
   `npx --no-install eslint app/static/js/inat_finder.js`, and spot-check the
   affected modes against live iNaturalist API responses before deploying.

- Flask app factory in `app/__init__.py`; extensions initialized in `app/extensions.py`.
- All API responses use JSON; the frontend is a SPA-style UI talking to `/api/` endpoints.
- FASTA sequence headers are sanitized on input and restored on download/display (see `fasta_utils.py`).
- RAxML-NG jobs use named presets (`fast_good`, `standard`, `publication`, `maximum`) defined in `tree_builder_service.py`.
- **`MAFFT_DIRECTION_MODE` chooses which direction check the normal alignment
  runs**: `fast` (`--adjustdirection`, the default), `accurate`
  (`--adjustdirectionaccurately`) or `off` (no flag). It is validated at import
  time and an unrecognised value falls back to `fast` with a warning -- never
  to `off`, which would silently disable direction correction. It does not
  override the per-job `fix_orientation` setting: false there still means no
  direction flag whatever the mode is. `accurate` exists so the change can be
  rolled back in production by setting one environment variable, without a
  deploy. Deliberately scoped to `_run_mafft()`; `fix_direction_with_mafft()`,
  the direction-only pre-pass MUSCLE/Clustal Omega/IQ-TREE depend on, stays on
  the accurate check because there MAFFT's answer is the only direction signal
  there is. Every invocation logs `event=alignment.mafft_completed` with its own
  elapsed time, `outcome` (`success`/`failed`) and `_R_` count. Emitted from a
  `finally` around the subprocess only, so: a veto rerun is measured
  separately; a run that times out having produced nothing -- the one whose
  duration matters most -- is still recorded as `outcome=failed`; a MAFFT run
  whose `_R_` markers then fail to parse is recorded as `outcome=success` with
  `aligner_reversed_count=unknown` (never `0`, which would read as "reversed
  nothing"); and a `publish_command` failure records nothing, because MAFFT
  never started.
- **ORIENT-vs-MAFFT disagreement is header-matched, not count-matched.** MAFFT
  names the records it reversed through its `_R_` headers, so the disagreement
  is `flipped - orient_uncertain - orient_never_saw` over sets, published as
  `aligner_reversed_count` / `orient_uncertain_count` /
  `aligner_orientation_disagreement_count`. Only a genuine contradiction is a
  `DEGRADED`; a flip of a record ORIENT declined to call is an INFO line. When
  the ORIENT headers were not persisted the answer is reported as unknown
  (`disagreement=None`, `basis=counts`) rather than guessed from two totals.
- **Quick Tree refuses individual sequences over
  `QUICK_TREE_MAX_SEQUENCE_BP` (10,000 bp).** The constant and the
  preset-detection live in `app/services/tree_parameter_validation.py`; the
  browser mirrors it in `sequence_entry.html` and the two are asserted equal by
  `tests/test_quick_tree_limits.py`. Which submissions are capped is decided by
  the explicit `submission_mode` the Tree Builder posts -- both Quick Tree
  buttons send `"quick_tree"`, the advanced form sends `"advanced"` -- with an
  exact match of `QUICK_TREE_PRESET` as the fallback for a body that carries no
  marker, or one nobody recognises (a cached page, a script, a typo -- falling
  back rather than failing open is deliberate, since a mistyped marker would
  otherwise switch the guardrail off silently). Only an explicit `"advanced"`
  opts out. "Uses the preset's tree method" is **not** the test: a
  deliberate limited IQ-TREE + MUSCLE + no-trimming request is an advanced
  submission, and the advanced form can reproduce the preset's four values by
  hand, which is why the marker exists.

  **It is a guardrail, not an abuse boundary.** A caller who declares
  `advanced` is not capped, deliberately: the advanced builder has always
  accepted a 150 kb locus, so declaring advanced mode opens nothing that was
  not already open. What the cap prevents is the accident -- a genome pasted
  into the two-click preset meant for barcode reads. A real ceiling on pipeline
  cost would have to be tied to the work itself (total bases x sequence count
  against the aligner that will run), not to which button was pressed. The v1
  API is untouched.
- **Quick Tree runs `iqtree_fast`, not FastTree.** Since 2026-09-17 the preset
  is IQ-TREE 3 `-n 5 --alrt 1000` under a fixed GTR+G, run by `_run_iqtree(…,
  fast=True)` — on real job alignments it finds trees 10 to 200 log-likelihood
  units better than FastTree at the same wall time (2-40 s). Node labels are
  SH-aLRT **percentages (0-100)**, not FastTree's 0-1 SH-like values, so a
  reader who assumes the old scale is off by two orders of magnitude.
  `fasttree` remains a selectable advanced method and is unchanged.
- **Quick Tree deliberately omits UFBoot.** `_run_iqtree` forces `bootstrap = 0`
  in Quick Tree mode rather than trusting the caller, and the SH-aLRT count is the fixed
  `IQTREE_FAST_ALRT_REPLICATES` rather than `params.alrt_replicates`, because
  the preset has no support control to read one from.
- **Quick Tree sends no `bootstrap`.** Neither engine can run one: FastTree
  ignores it (`_run_fasttree` hardcodes `-boot FASTTREE_SH_RESAMPLES`, which is
  SH-like local support, not bootstrap proportions) and the `iqtree_fast`
  preset omits it. `tree_builder_service` records `bootstrap: None` for
  both, with `support_type: "sh_like"` and `"alrt"` respectively. `create_job`
  drops an unsent bootstrap for either method rather than persisting the
  generic 1000 default; an explicitly submitted one is still stored.
- **The IQ-TREE binary is `/usr/local/bin/iqtree3` (3.1.4).** `/usr/bin/iqtree2`
  is 2.0.7 and must not be used. `_run_iqtree` uses the version 3 spellings
  (`-T`, `--prefix`, `--seed`, `--redo`, `-B`, `--alrt`); 3.1.4 still accepts
  the 2.x short forms but nothing here should. Every place that normalises
  `iqtree2` to `iqtree` also normalises `iqtree3`: the method maps in
  `tree_analysis_service.py` and `tree_viewer_phylotree_v2.js`, and the tool
  regex in `security_events.py`. A non-fast run always pairs `-B` with
  `--bnni`, recorded as `bnni: true`.
- **RAxML-NG bootstrapping computes two support metrics, not one.**
  `_get_raxml_cmd` passes `--bs-metric fbp,tbe`, so RAxML writes
  `<prefix>.raxml.supportFBP` and `.supportTBE` and **no** bare
  `.raxml.support`. The Felsenstein tree is the one served as
  `tree_original.newick`, so the viewer's Bootstrap badge still means what it
  always did; the transfer-bootstrap tree is copied beside it as
  `tree_original_tbe.newick` (`tree_pruned_tbe.newick` after a recompute) and
  nothing displays it by default. `tree_metadata.json` records
  `bootstrap_metrics: ["fbp", "tbe"]` and `tbe_tree`. The bare `.raxml.support`
  name is kept as a fallback for a binary that ignores the flag — do not delete
  that branch. **The two files are on different scales**: RAxML writes FBP as a
  percentage (`93`) and TBE as a proportion (`0.930000`), so anything that ever
  displays the TBE tree must say which it is rather than reusing the bootstrap
  badge's reading.
- **Type-specimen tips are marked from two sources, by exact accession only.**
  `app/services/type_specimen_service.py` merges MycoMap's type list
  (`mycomap_type_specimens.json`, replaced weekly by
  `scripts/dikarya_refresh_type_specimens.py` via
  `ops/cron/dikarya-refresh-type-specimens`) with GenBank's own `/type_material`
  qualifier or RefSeq's "from TYPE material" definition marker
  (`genbank_type_material.jsonl`, appended by `_parse_genbank_xml()` on every
  GenBank fetch and backfilled by the same script). Both live in
  `Config.TYPE_SPECIMEN_DIR` (`cache/type_specimens`, tree:dikarya 2775 like
  `cache/blast`). Neither source contains the other: the MycoMap list holds no
  RefSeq `NR_` records, and 93% of the `NR_` accessions in existing jobs are
  types. Never match on organism name -- that marks every sequence of a species
  as its type -- and resolve accessions through `record_accession()` so a
  Mushroom Observer `MO123456` label is never read as a GenBank accession.
  "reference material" is not type material and is deliberately not marked.
  Resolution happens when the page is served (`type_specimens_for_job()` ->
  `window.TYPE_SPECIMENS`), never written into a job, so old jobs pick up
  markers as the data grows and tree state/undo are untouched.

  Three pairs are mirrors and must change together: `resolve_tip()` /
  `_typeSpecimenForName()`, `append_type_status()` /
  `appendTypeStatusToLabel()`, and `classify_type_material()`, whose statuses
  the viewer shows verbatim. The viewer draws a bold label plus a gold
  superscript "T" `<tspan>` re-added by the node styler after every phylotree
  redraw (which wipes the label's children), with inline styles so image
  exports carry it. The "Type status in labels" Export option (on by default)
  appends `(holotype)` etc. to Current Newick client-side and to Original
  Newick/NEXUS via `?type_labels=1`. That parameter is opt-in on purpose: the
  viewer itself loads `/download/tree/newick` and matches tips by name, so a
  server-side default would break it. The labelled Original Newick is edited
  as text by `tree_io.relabel_newick_text()`, touching only the type tips'
  labels -- a Biopython round trip rounds every support value to two decimals,
  and that download promises the builder's own file.
- **GenBank accession policy lives in `fasta_utils.GENBANK_ACCESSION_RE`.** The
  large-scale INSDC families (WGS contigs, TSA transcripts, TLS targeted-locus
  records) share one accession structure and the string does not say which is
  which, so Dikarya accepts the syntax rather than the family -- NCBI runs TLS
  projects for ITS/ITS2, so an individual TLS record is often exactly what this
  app is for. The real shapes are 4 letters + 8-10 digits and 6 letters + 9-11
  digits (project code + 2-digit assembly version + contig digits); the
  intermediate counts are valid and were previously rejected.
  `_NON_INSDC_LARGE_SCALE_PREFIXES` excludes `INAT` by name, because "iNat" + 9
  digits is shape-identical to a 4+9 accession and 318,227 records on disk are
  exactly that against three real large-scale accessions ever submitted.
  `is_insdc_master_accession()` recognises a project/master record
  syntactically (every digit after the assembly version is zero); those carry no
  sequence and are refused at the accession entry points with an explanation
  rather than failing later as "NCBI could not resolve this". Per-sequence size
  is bounded by the existing `MAX_CUSTOM_GENBANK_SEQUENCE_BP`, applied on both
  user-facing accession paths -- no accession-family-specific limit.
- **`MO123456` is ambiguous and dedup is destructive.** "MO" is a real INSDC
  prefix, so the compact token is both a Mushroom Observer tip label and a
  valid 2+6 accession. `extract_mycomap_observation_reference(...,
  allow_compact_mo=False)` suppresses only that form; `mo:123456`, `MO #123456`,
  a mushroomobserver.org URL and the spelled-out site name have no accession
  shape and are always honoured, including inside a GenBank record's
  qualifiers. `sequence_dedup_service.record_provenance()` decides which to use
  from `source`/`hit_source` metadata, falling back to the version suffix
  (`MO123456.1` is GenBank; a Mushroom Observer label never carries one).
  `record_accession()` uses the same rule, so a MycoMap local hit's `MO######`
  internal id is never sent to NCBI as an accession.
- **`observation_reference_from_record()` refuses to guess.** It reads every
  trusted field (DEFINITION plus the `OBSERVATION_QUALIFIERS`) and uses the
  answer only when they agree on exactly one observation; two distinct
  references log `event=dedup.genbank_reference_ambiguous` and yield nothing.
  "Distinct" is counted *within* a field as well as across fields, via
  `extract_mycomap_observation_references()` (plural) -- a single `/note`
  reading "sequenced from iNat 280384724; compare iNat 999999999" is exactly
  as undecidable as two fields disagreeing. The whole-record blob is a
  fallback, never an override, and is held to the same rule. The singular
  `extract_mycomap_observation_reference()` stays on the per-record hot path
  and short-circuits; the two share one set of patterns and a test asserts
  `plural[0] == singular`.
- **The observation dedup's NCBI lookup runs in the worker, not in the
  request.** `dedupe_by_observation` / `apply_observation_dedup` default to
  `resolve_genbank_references=False`; `prepare_phylo_job_params` (which runs
  inside `POST /api/job`) keeps the offline grouping and `run_phylo_job` does
  the resolving pass before the INPUT step. `record_dedup_details` accumulates
  across passes, so the second pass cannot erase the first pass's removed
  records -- which the "rebuild including duplicates" action needs.

  **Anything that removes records must then call `apply_input_warnings()`**
  (`app/workers/queue.py`). The degenerate-input warnings depend on the record
  count and quote it in their text, so a pass that collapses three records to
  two both earns a warning and invalidates any existing one. The worker
  refreshes `job_params` *and* the `Job.metrics` copy, because
  `app/main/routes.py` renders the status page from the latter.
- **Job IDs come in two shapes and both stay valid forever.** Jobs minted
  before 2026-09-09 are UUID4; new ones are a short lowercase base36 string
  (`/job/aq7c/view`), minted by `generate_job_id()` in
  `app/services/job_id_service.py`. Nothing rewrites the ~11.6k UUID jobs on
  disk. Always validate with `validate_job_id()` before using an id in a file
  path — it accepts both and admits neither a dot nor a slash. The JS mirror is
  `JOB_ID_RE` in `sequence_entry.html`; change the two together.
  `generate_job_id()` starts at 4 characters and widens on its own once a
  length gets crowded, so never assume a fixed length.
- **Every path that creates a job must mint through `generate_job_id()`.**
  There are five, not one: `create_job` and the duplicate-rebuild endpoint in
  `app/api/routes.py`, `app/api_v1/routes.py`, and the iNat and Mushroom
  Observer preparation flows in `inaturalist_tree_service.py` /
  `mushroom_observer_service.py`. A path still calling `uuid.uuid4()` keeps
  handing out long URLs and nothing fails, so the miss is invisible until
  someone reads a URL -- which is how the iNat flow shipped long ids after the
  other three were converted. `enqueue_mycomap_blast_refresh_job()` in
  `workers/queue.py` deliberately keeps a UUID: that id is an internal RQ
  handle, never a job directory or a URL.
- **Job ids are not treated as secrets.** A short id is guessable (36**4 =
  1.7M at 4 characters), and that is a deliberate, accepted trade for short
  links: there is no guess-rate limit on the job surface, and `_job_ref()` in
  `app/monitoring/services.py` publishes an 8-character prefix on the
  unauthenticated monitoring views as it always has. Do not add hashing or
  throttling back on the theory that an id is a capability token. The rest of
  the monitoring rules still hold -- no sequence headers, notes, outgroup or
  other submission-derived text on those views.
- **Never call `Phylo.write()` for a file under `var/jobs/<id>/tree`.** Use
  `write_tree_file()` from `app/services/tree_io.py` (still re-exported from
  `tree_edit_service.py`). Biopython gets *two* things wrong here:
  - Its default `"%1.5f"` branch-length format rounds anything under 5e-6 to a
    hard zero, and these trees carry nine decimal places with hundreds of
    branches at 6e-9 — a single prune or reroot used to manufacture
    zero-length branches that read as identical sequences everywhere
    downstream.
  - Its **NEXUS writer** emits `TAXLABELS` unquoted and space-separated, so a
    label containing a space, comma, parenthesis or semicolon — i.e. almost
    every fungal label — produced a file no NEXUS reader could parse.
    `tree_io.write_nexus_tree()` replaces it: labels are quoted by
    `quote_tree_label()` and appear only in `TAXLABELS`/`TRANSLATE`, with the
    tree string referring to taxa by integer (what MrBayes and PAUP* do), so a
    parenthesis in a label cannot break the parse.

  `quote_tree_label()` quotes anything not purely alphanumeric. That is
  stricter than Newick alone needs and deliberately so: the same helper backs
  `restore_tree_names()`, which also rewrites NEXUS files, where `-` and `=`
  are punctuation and a bare `_` reads as a space.
- **Do not add characters to `NEWICK_UNSAFE_TIP_CHARS`.** It holds only
  `\r\n\0` — the characters no download can carry under any quoting, because
  they end a FASTA header and a Newick label outright. Structural punctuation
  is deliberately absent: `quote_tree_label()` carries `()[];,:` and both quote
  characters through Newick and NEXUS, a FASTA header restricts nothing but the
  line break, and the pipeline itself puts all of them into labels (96% of jobs
  on disk have at least one). Banning them only stopped a user from retyping a
  name the tree was already showing. `validate_tip_rename()` folds a pasted tab
  or newline into a space rather than refusing the edit; only an all-control
  name is rejected. The v1 API's `_validate_tip_name` mirrors this and must
  change with it.
- **The NEXUS download is rebuilt, not served off disk** — see
  `build_nexus_download()` in `tree_io.py`, used by both `/api/job/<id>/download/tree/nexus`
  and v1's `tree.nexus`. 82% of the `tree_*.nexus` files in `var/jobs` were
  written by Biopython's TAXLABELS writer and do not parse, and
  `tree_pruned.nexus` exists for only ~5% of the jobs that have a
  `tree_pruned.newick`, so the old code also served the *unpruned* tree under
  the current tree's name. The Newick beside it is the source of truth; the
  stored NEXUS is used only when it is valid and no older. Nothing is written
  back, so this stays clear of `tree_state` locking and the undo snapshot.
- **A format that genuinely cannot hold a label must ship the key.** MrBayes is
  the only one: a NEXUS matrix label is whitespace-delimited, so the run uses
  `SEQnnnnnn` ids from `sanitize_fasta_headers()`. The download bundles
  `sequence_names.tsv` mapping them back — written beside the run by
  `_run_mrbayes`, and reconstructed by position from the alignment
  (`reconstruct_name_map()`) for jobs that predate it. Do not drop a user's
  labels from a download without including a decoder.
- **Any file a tree edit writes must be listed in `SNAPSHOT_PATHS`** in
  `app/services/tree_undo_service.py`. Undo restores a snapshot of
  `tree_state.json` and `tree/tree_pruned.{newick,nexus}` taken before the edit;
  a fourth file left out of that list would be half-undone. Conversely, a NEW
  endpoint that writes tree state but is not undoable must call
  `clear_undo_checkpoint()`, or a later Undo silently reverts it. See the
  "Single-level undo of a tree edit" section of `ARCHITECTURE.md`.
- Collapsing a clade in the viewer is display state only (phylotree's transient
  `node.collapsed`). It must never prune, persist, or trigger a recompute.
- Use existing Tailwind utility style patterns from `templates/sequence_entry.html` and `templates/partials/*.html`.
- Reuse modal structure from `templates/partials/add_sequences_modal.html`.
- Support dark mode (`dark:*` classes) for all new UI.
- Reuse existing color/style tokens (`journal-dark`, `journal-gold`, etc.).
- `showStatus()` is global and provided by `templates/base_modern.html`.

## Change Strategy for AI Agents (Minimal Churn)

- **Do NOT use TDD** — just make the requested changes. The human developer will test the code manually.
- Prefer targeted edits and minimal, production-safe modifications over large refactors.
- Prefer small helper functions over large rewrites when adding features.
- Do NOT split large inline scripts into separate JS files unless explicitly requested by the user.
- Preserve existing DOM IDs and event handler wiring whenever possible.
- Preserve queue behavior strictly:
  - Count badge updates.
  - Empty/non-empty state toggles.
  - Clear/remove actions.
  - Outgroup dropdown refresh (`populateOutgroupDropdown`) after queue changes.
  - Duplicate handling in `addSequences(...)`.

## Reading logs

The `tree` user cannot read the systemd journal (it is in neither `adm` nor
`systemd-journal`, deliberately — that would grant read access to the whole
journal, including sshd auth records). Use these instead, in this order:

| What you want | Where it is | Readable directly? |
|---|---|---|
| **What is actually broken** | `var/logs/errors.log` (WARNING+ only) | yes |
| Daily summary of failures/degradations | `~/.dikarya/log-digests/<date>.txt` | yes |
| Per-job pipeline detail | `var/jobs/<id>/logs/{pipeline,alignment,tree_builder}.log` | yes |
| Gunicorn access/errors | `var/logs/{access,error}.log` | yes |
| Worker app output | `var/logs/worker.log` (phylo_high), `var/logs/worker-bulk.log` (phylo_bulk) | yes |
| Internet-wide scanner sweeps | `var/logs/scanner.log` | yes |
| Unit lifecycle, OOM kills, start failures | journal, via the wrapper below | wrapper only |

**Nothing in `var/logs/` is deleted any more.** `ops/logrotate/dikarya` used to
say `rotate 14` with `maxage 30`, and `rotate 14` was what actually bound: every
stem kept 14 rotations plus the live file, which came to 13 days rather than the
30 the `maxage` implied, because `maxsize 25M` makes a busy day rotate twice and
burn two slots. Thirteen days of *all* of these logs was 3.2 MB compressed
against a 5.3 GB `var/jobs`, so the retention bought nothing and cost history --
`errors.log` alone is 2.8 KB/day, about 1 MB/year. `rotate` is now set past any
reachable value and `maxage` is gone. `var/metrics/system_metrics.jsonl` still
ages out at 30 days on purpose: it is machine telemetry nothing reads back, not
a record of what happened.

### Scanner noise vs probes aimed at this app

About two thirds of this host's requests are 4xx, and nearly all of that is
internet-wide vulnerability sweeps. `app/services/security_events.py` splits
them, and `classify_request_failure()` is the single place that decides:

- **scanner** -- a probe for software this host does not run (`/.env`,
  `/.git/config`, `/wp-admin/...`) or a verb it does not serve. The HTTP method
  reaches `classify_request_failure()` intact (bounded and stripped to A-Z by
  `normalize_method()`, never renamed), so `PROPFIND`, `TRACE` and `CONNECT`
  are reported as `reason=unsupported_method` rather than all collapsing into
  one anonymous bucket -- the verb is the sweep signature. The verb is checked
  before the status, because a PROPFIND sweep answers 405 on nearly every path
  it tries. A scanner classification is filed to `var/logs/scanner.log`
  whether or not a Flask route matched; a matched route still keeps its
  ordinary `http.request_failed` line. Written to `var/logs/scanner.log`
  as `event=security.scanner` on the `dikarya.scanner` logger, which has
  `propagate = False` so this volume can never reach `errors.log` or the worker
  console. Kept rather than dropped: a sweep is evidence when the same IP later
  does something targeted.
- **targeted** -- someone mapping *this* application: path traversal, a null
  byte, an injection marker, a malformed job id, or an unmatched path under
  `/api/` or `/admin/`. Logged at WARNING as `event=security.suspicious`, so it
  lands in `errors.log`. Find them with
  `grep security.suspicious var/logs/errors.log`, or read the digest's
  "App-targeted probes" section.
- **neither** -- an ordinary user 4xx keeps its existing
  `event=http.request_failed` line, unchanged.

`install_scanner_log()` sets the logger's level and `propagate = False`
**before** it touches the filesystem, and re-asserts them on an already
configured logger. They used to be the last two statements, so a read-only or
full `var/logs` raised `OSError` out of `mkdir()` with the logger still
propagating and handler-less -- every scanner record then fell through to the
root logger, which is the WARNING+ `errors.log` mirror. That is the opposite of
the intended fail-safe, and it fires exactly when it hurts most.

The digest counts a `security.suspicious` record **once**, in the App-targeted
probes section. It used to fall through into the generic exception tally as
well, where `meaningful_error_key()` rendered it as an unreadable
`event=security.suspicious method=<...>` row that crowded out real failures.

### Behaviour scoring: catching someone who is good at this

Everything above judges one request by its URL, which is why it catches sweeps
and would never catch anyone competent. Somebody who reads `openapi.json`,
notices job ids are four base36 characters and starts walking
`/api/job/<id>/download/alignment` sends requests that are individually
indistinguishable from a real user's. Three mechanisms cover that, and the
first two share one Redis window (the same short-timeout client the
missing-route gate uses, so a wedged Redis costs 100ms, never a request):

**`app/services/security_actors.py` scores the actor, not the request.** Each
signal is worth little once and a lot repeated -- that is what `free` encodes,
the allowance that scores nothing because that many is ordinary use. One user
hits one missing job; nobody hits nine. One `WARNING`
(`event=security.actor_escalated`) fires when the total crosses
`ESCALATION_THRESHOLD`, then that actor is silent for an hour.

- Actors are scored per **network** (/24 or /64), not per address, because
  rotation is free otherwise. The exact addresses ride along as the
  zero-weight `client_ips` signal. A large NAT is therefore one actor: accepted,
  since the line is a WARNING whose evidence names the addresses.
- `api_surface_probe` and `admin_probe` **no longer WARN individually** -- they
  fired 61 times in one ordinary day, essentially all `.env` hunting. They are
  weight-1 with a hard cap, arranged so both saturated together stay *below*
  the threshold: a dumb sweep can never escalate on volume alone.
- An escalation line carries signal **counts only**. No path, no query, no
  agent string -- same rule as the monitoring views.
- **A new signal must define `free` deliberately.** `free=0` means "one
  occurrence is an attack", which is true of a honeytoken and false of almost
  everything else. Getting that wrong is how this becomes noise again.

**`app/services/security_path_crowd.py` decides what is boring by counting who
asks.** A path requested by ≥`DICTIONARY_CLIENTS` unrelated clients is sweep
vocabulary by definition (`/api/.env` came from nine in a day) and scores
nothing; a path requested by exactly one client, ever, that also looks like
this app's surface, is a guess about *us* and scores. A scanner cannot produce
a singleton, because its dictionary is shared with every other scanner on the
internet. Paths are normalised (ids and digit runs collapsed) and stored only
as a hash. Without Redis the verdict is `emerging` -- no opinion, never
`singleton`, so the failure mode is quiet rather than accusatory.

**`app/services/security_honeytokens.py` plants paths that exist only in our
own output.** A scanner's dictionary was fixed before it ever contacted this
host, so it cannot ask for something it learned here. Three today: a
`Disallow:` line in `robots.txt`, a decoy job id in a `job_viewer.html`
comment, and a deprecated stub in the OpenAPI document. Each is published in
exactly one place, answers an ordinary 404 so a prober cannot tell it tripped
one, and escalates on its own. `DECOY_JOB_ID` is in
`job_id_service.RESERVED_JOB_IDS` and **must stay there** -- minting it for a
real job would report that job's visitors as attackers.
`tests/test_security_honeytokens.py` asserts both the reservation and that each
token is still planted where it is published; a removed plant is a tripwire
that silently stops working.

### Attacks aimed at Dikarya specifically

Four reason codes exist for attacks on *this* app rather than on whatever
answers on port 443, and all four WARN immediately:

- `path_traversal_app_surface` -- traversal that starts from a job artifact
  route rather than the site root. A sweep asks every host for `/etc/passwd`
  (still reported, as plain `path_traversal`); only someone who has looked at
  Dikarya climbs out of `/job/<id>/download/`.
- `artifact_path_probe` -- a request naming the on-disk layout
  (`input_info.json`, `tree_state.json`, `var/jobs/...`). Those names are in
  this repository and in no scanner dictionary anywhere.
- `tool_exploit_probe` -- a request naming the binaries the pipeline executes
  (MAFFT, RAxML-NG, IQ-TREE, trimAl, MrBayes, BLAST) or shaped like an attempt
  to smuggle an argument into one. This is the part of Dikarya that actually
  runs things.
- `path_escape_refused` / `argument_injection_refused` -- reported by the code
  that **refused** the attempt, not by reading a URL. The attacks that matter
  most here are invisible in the request line: a path that only turns out to
  escape `var/jobs` once resolved, a symlink planted in a job directory, a
  model string that would have reached a RAxML `--model` argv as a flag.

The last two are why `note_attack_attempt()` and
`note_tool_argument_refusal()` exist in `request_diagnostics.py`. Anything that
refuses a weaponized value calls one of them; inside a request it becomes an
actor signal, and in the worker (where there is no actor) it is written
straight out as a WARNING with the job and user from the log context. Two rules:

- **The artifact and toolchain checks only run on an UNMATCHED path.** Real
  routes legitimately carry these words -- `/job/<id>/download/mrbayes` is a
  download, `/files/<path:filename>` serves arbitrary names -- so testing a
  matched route would report the app's own traffic as an attack.
- **Report the attempt, not the typo.** `looks_weaponized()` in
  `security_events.py` is the line: a leading dash, a shell metacharacter, a
  traversal sequence, a path separator outside a RAxML brace block. A
  misspelled model name is a user mistake and must stay silent, or the signal
  is worthless. Validators refuse plenty of ordinary errors.

Three rules when editing this:

- **A plain 401/403 on a real route is not a targeted signal.** It is almost
  always an ordinary authorization outcome, and an earlier version that treated
  it as one also *replaced* the normal `http.request_failed` record, so its
  developer reason code (`scope_required`, `csrf_token_missing`) never reached
  any log. The security record only ever adds; it must never short-circuit the
  ordinary diagnostics for a matched route.
- **A valid-shaped job id that 404s is not a probe.** Ids are short and
  guessable by design (see the job-id conventions above); only an id
  `validate_job_id()` refuses is worth reporting.
- **The query string is classified but never logged.** It carries sequence text
  and search terms. Only `request.path` is written, scrubbed of control
  characters so an attacker-controlled path cannot inject a log line.

The scanner path lists live in `security_events.py` and
`scripts/dikarya_log_digest.py` imports them, so the digest's idea of noise and
the app's cannot drift. The module is deliberately free of Flask imports to keep
that import safe.

**RQ's `cleaning registries for queue` heartbeat is filtered out** in
`install_rq_logging()`. It was 400 of 834 lines in `worker.log` -- 48% of the
file -- said only that the worker was alive, and dragged the digest's worker
context coverage down to 60% because RQ's own records carry no job id. A real
maintenance failure logs at WARNING and is unaffected.

**Start with `errors.log`, not `error.log`.** Despite its name, `error.log` is
Gunicorn's combined stream and runs ~98% INFO — real failures are buried in it.
`errors.log` receives WARNING and above only. Nothing is removed from
`error.log`, so the full history is still there when you need context around a
failure.

**Failed upstream API responses are retained separately.** Search `errors.log`
for `event=api.response_failed diagnostic=<id>`, then read
`var/logs/api-responses/<UTC-date>/<id>.json.gz` with `gzip -dc`. Each archive
contains the full redacted response body, HTTP status, selected response headers,
timestamp, endpoint (without query credentials), and request/job context.
HTTP 200 responses rejected by observation validation are included, as are failed
HTTP retry attempts. Network failures record that no response body was available.
Credentials are redacted; request bodies and authorization/cookie headers are
never archived. Archives are compressed, not truncated or automatically expired.
They are outside the static/download trees. Do not paste an entire archive into
a public issue; it can contain observation data and account context.

New upstream calls should use `diagnostic_urlopen` from
`app/services/api_diagnostics.py`, or `record_requests_failure` for requests/httpx
responses. When HTTP succeeds but semantic validation fails later, explicitly
call `record_api_failure` with the response that failed validation. Do not
re-fetch the observation for diagnostics: it may have changed. Archive failures
emit `event=api.diagnostic_write_failed` and must never mask the original error.

Every log line emitted inside a request carries its origin:

```
[2026-08-14 08:01:02] [WARNING] [app.api.routes] ... [req=2abb1286 user=someone@example.com job=5db685aa-...]
```

so "who hit this error, on which job?" is a grep rather than a correlation
exercise against `access.log` plus a database query. Lines logged outside a
request (worker startup, CLI) carry no context suffix.

**`DEGRADED` marks work that completed with less than was requested** — a tree
built without its NCBI references, rooting that could not be reapplied, subprocess
limits that could not be applied. These previously looked like success in the
logs. Find them with:

```bash
grep DEGRADED var/logs/errors.log
```

Add new ones with `log_degradation()` from `app/services/log_context.py` rather
than a bare `logger.warning`, so they stay countable.

**The digest replaces ad-hoc `awk`.** It reports non-2xx by endpoint, grouped
exceptions with affected users, degradations, 429s, slow endpoints, and the
heaviest clients:

```bash
.venv/bin/python scripts/dikarya_log_digest.py --hours 24
.venv/bin/python scripts/dikarya_log_digest.py --hours 168
# A job is only listed as having no terminal event once it is older than this
# (default 60 minutes), so a running RAxML job is never called orphaned.
.venv/bin/python scripts/dikarya_log_digest.py --hours 24 --unterminated-grace-minutes 240
```

The worker section reads **both** worker streams — `worker.log*` and
`worker-bulk.log*` — merged by timestamp. It globbed `worker.log*` alone until
2026-09-07, so every job that ran on the bulk queue was simply absent from the
digest, successes and stalls alike. A new stream needs adding to `WORKER_STEMS`
in the script.

`retried` counts reattempts; `deferred` counts planned waits, where the task
returned `rq.Retry` on purpose to wait for an upstream result and logged
`event=job.deferred` before doing so (the MycoMap NCBI rerun is the only one
today). Both produce a second `event=job.started`, which is why the marker is
needed to tell them apart — without it a normal MycoMap wait was reported as a
retry. A new deliberate `rq.Retry` must log `event=job.deferred` in the same
edit, or it will be counted as a failure reattempt.

### Reviewing only logs not reviewed before

Repeated agent log reviews use the durable checkpoint at
`.log-review-checkpoint.json`; conversation history, daily digest timestamps,
file mtimes, and log-rotation boundaries are not review checkpoints.

At the start of a review, run:

```bash
.venv/bin/python scripts/dikarya_log_digest.py --since-checkpoint
```

This reads the last successfully reviewed boundary without changing it and
prints an exact half-open UTC window plus a line such as
`checkpoint_candidate=2026-08-22T18:03:00Z`. Investigate and report everything
in that window. Capture the candidate exactly as printed; do not substitute the
time when the investigation finishes, because events arriving during the
review belong to the next review.

Only after the review has completed successfully, advance the checkpoint:

```bash
.venv/bin/python scripts/dikarya_log_digest.py \
  --mark-reviewed 2026-08-22T18:03:00Z
```

Never advance the checkpoint for a failed, interrupted, partial, or merely
started review. The next run will intentionally cover the same records again.
To audit a particular historical interval without touching the checkpoint, use
`--since <ISO-8601 UTC> --until <ISO-8601 UTC>`. To initialize a missing
checkpoint, complete an initial explicit review, then mark its printed
`checkpoint_candidate`.

The window governs which files are opened, not just which records count: only
the live file, rotations that overlap the window, and the one rotation
immediately before it are read, so a 24-hour report does not decompress two
weeks of history. The coverage footer reports `scanned=` (lines read) separately
from `in-window=` (records inside the window), and percentages use the latter.

A cron entry (`ops/cron/dikarya-log-digest`) writes it daily at 07:05 UTC. Output
goes to `~/.dikarya/log-digests/` because `var/logs/` is `dikarya`-owned and the
`tree` user cannot write there.

The access log records **real client IPs** (via `ProxyFix`; it logged `127.0.0.1`
for everything before) and **request duration in microseconds** as the last field
(via `gunicorn.conf.py`, auto-loaded from the working directory).

```bash
# Scoped, read-only journal access. Usage: <unit-keyword> [lines]
sudo /usr/local/sbin/dikarya-journal worker          # defaults to 200 lines
sudo /usr/local/sbin/dikarya-journal web 1000
sudo /usr/local/sbin/dikarya-journal metrics 500 | grep -i error
```

Only `web`, `worker`, and `metrics` are accepted, and the second argument must be
an integer (capped at 5000). The wrapper deliberately forwards **no other
arguments** — `journalctl` ORs its `-u` flags, so passing arbitrary arguments
would let `-u ssh.service` read the entire journal and defeat the scoping. Filter
with your own `grep`/`awk` pipeline instead; that needs no privilege. Source is
`scripts/dikarya-journal`.

**Reach for the journal specifically when a process died rather than logged.**
systemd's own messages (`Main process exited, code=killed, status=9/KILL`, OOM
kills, drop-in start failures) never appear in application stdout, so
`worker.log` will not show them. A clean `Deactivated successfully` in the
journal means a deliberate restart, not a crash.

Note that a web or worker **restart produces a brief 502** while Gunicorn is
down (~3-4 seconds). A 502 that succeeds on retry, with a matching
`Stopping...`/`Started` pair in the journal, is a restart window and not a bug.
That window stays short only because `post_worker_init` in `gunicorn.conf.py`
makes SIGTERM raise `sse_registry.begin_shutdown()`, which closes open SSE
streams at once. The master closes its listener *before* draining workers, so
without it every restart was a full 30s (`graceful_timeout`) of site-wide 502s.

**Worker and metrics processes get their stdout/stderr handlers from
`app._install_logging()`, not from `logging.basicConfig()`.** The root logger
carries the WARNING-only `errors.log` mirror, which made RQ's
`_has_effective_handler()` skip installing its own handlers and made
`basicConfig()` a no-op that silently left the root logger at WARNING — killing
every INFO record including `event=job.started`. Non-Gunicorn processes now get
an explicit stdout (INFO+) / stderr (ERROR+) pair; Gunicorn processes
deliberately get none, because Gunicorn already owns those streams. If a new
process type stops logging, look there first.

`var/logs/worker.log` is fed by `StandardOutput=append:` in
`/etc/systemd/system/dikarya-worker.service.d/logging.conf` (source:
`scripts/dikarya-worker-logging.conf`). It only captures output from processes
started *after* that drop-in was loaded — if the file is empty while output is
still appearing in the journal, the running worker predates the config and needs
a restart (check for in-flight jobs first). Rotation is handled by
`ops/logrotate/dikarya`; the file must stay `dikarya`-owned so the in-process
logrotate in `app/services/log_rotation.py` can truncate it.

## The monitoring dashboard

`/admin/monitoring` shows live pipeline detail for every running and queued
job: which step each one is on and for how long, the tool's own progress
counter (RAxML bootstrap replicate, IQ-TREE iteration, MrBayes generation),
CPU/RSS of the processes on the worker, the last file the job wrote, how much
of its RQ timeout budget is gone, queue depths and per-worker state. It is
rendered by one JavaScript renderer fed by `/health/jobs`, from an inline
snapshot on first paint and from a 5-second poll after that, so the two views
cannot drift apart.

**The page and `/health/jobs` are unauthenticated, so nothing here may emit
anything derived from a submission**: no sequence headers, no notes, no
`outgroup`, no file contents. Options come from `PUBLIC_JOB_OPTION_KEYS`, a
whitelist rather than a filter, because `input_info.json` holds the submitter's
own text right beside them; progress lines are matched by regexes that capture
numbers only, because the tool logs they come from also contain taxon labels.
Keep new fields on that side of the line.

The job id itself is **not** on that list — ids are not treated as secrets here
(see the job-id conventions above). `_job_ref()` still truncates to 8
characters, but that is a display choice that keeps the table readable, not a
security boundary, so emitting a full id would be untidy rather than a leak.

A queued job has no job directory yet — the worker creates it — so its summary
falls back to the description RQ already stored, which `safe_job_description()`
built from counts and option names for this same reason.

## Ops / Debugging Notes

- Production runs under systemd (Gunicorn web + worker services).
- If route changes do not appear in production, Gunicorn reload/restart may be required.
- `127.0.0.1` is not reachable for website tests in this environment; use `https://dikarya.us` for local website testing instead.
- Check app logs for 500 errors before changing code (see "Reading logs" above).
- `py-spy` is installed at `/usr/local/bin/py-spy`. Use it before restarting a hung
  web process, because a restart destroys the evidence:

  ```bash
  # What is every thread of a worker doing right now?
  sudo /usr/local/bin/py-spy dump --pid <gunicorn-worker-pid>
  # Live top-style view
  sudo /usr/local/bin/py-spy top --pid <gunicorn-worker-pid>
  ```

  **Agents cannot run this themselves.** Gunicorn workers run as the `dikarya`
  user, py-spy needs ptrace (root), and the `tree` user's passwordless sudo covers
  only the four wrappers listed above and below. Ask the human to run the dump and
  paste the output (in Claude Code they can prefix the command with `! ` to run it
  in session). If this becomes routine, add a root-owned wrapper following the
  same pattern as `dikarya-journal`, e.g. `/usr/local/sbin/dikarya-pyspy <pid>`,
  validating the PID argument rather than forwarding it blindly.

  Gunicorn runs `--workers 4 --threads 8` (verified against
  `/etc/systemd/system/dikarya-web.service` on 2026-08-24; this file previously
  said `--threads 2`), so **32 requests can be in flight at once**. A hang where workers are alive but idle (low CPU, few established sockets
  on `:8000`) means those slots are held by handlers that are sleeping rather than
  working — dump the threads to find which handler. Long-lived streaming endpoints
  such as `/api/job/<id>/events` (SSE) are the usual suspects.
- Jobs stuck in a non-terminal state (`queued`/`running`) keep SSE streams alive and
  consume request slots. Reap them with `flask reap-stuck-jobs` (see Commands).

## Documentation Split

- **CLAUDE.md** = implementation guidance, conventions, and safe-edit rules.
- **ARCHITECTURE.md** = deeper system design and reference details.

If making substantial changes, consult `ARCHITECTURE.md` for subsystem context, but keep code changes aligned with the conventions in this file.
