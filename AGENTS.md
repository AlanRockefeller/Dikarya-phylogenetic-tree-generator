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
  `tests/test_tree_analysis_metrics.py` — add a case there. The builder itself
  is resolved once, by `resolve_tree_support_context()`, which is what fills
  `window.TREE_METHOD`; do not resolve it separately in the template.
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
| Unit lifecycle, OOM kills, start failures | journal, via the wrapper below | wrapper only |

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
