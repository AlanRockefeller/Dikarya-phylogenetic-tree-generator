# Application Architecture

## High-Level Overview
This is a Flask-based bioinformatics application designed to handle sequence analysis jobs. It uses an asynchronous architecture to process long-running tasks like sequence alignment and phylogenetic tree building.

## Technology Stack

### Core Frameworks
- **Web Framework**: Flask
- **WSGI Server**: Gunicorn
- **Language**: Python 3

### Data & State
- **Database**: PostgreSQL (via `psycopg2-binary`)
- **ORM**: Flask-SQLAlchemy
- **Migrations**: Flask-Migrate (Alembic)
- **Queue/Cache**: Redis

### Asynchronous Processing
- **Queue Manager**: RQ (Redis Queue)
- **Monitoring**: `psutil` (system resource monitoring)

### Bioinformatics
- **Library**: Biopython
- **Job Types**:
    - Sequence Orientation (auto-fix reversed ITS sequences)
    - Sequence Alignment (supported methods: MAFFT, MUSCLE, ClustalO, etc.)
    - Tree Building (supported methods: NJ, RAxML, IQ-TREE, MrBayes)
    - Trimming (supported methods: trimAl, BMGE)

### Authentication
- **Library**: Flask-Login
- **Security**: Werkzeug password hashing

## Database Schema
The application uses a relational database model defined in `app/models.py`.

### Models
- **User**
    - `id`: Primary Key
    - `email`: Unique identifier
    - `password_hash`: Hashed password
    - `created_at`: Timestamp
    - *Relationships*: One-to-Many with `Job`

- **Job**
    - `id`: Primary Key (String, likely UUID)
    - `user_id`: Foreign Key to `User`
    - `status`: Job state (e.g., "queued", "running", "completed", "failed")
    - `input_type`: Type of input data
    - `job_dir`: File system path to job results
    - `metrics`: JSON field for storing job-specific metrics
    - `created_at` / `updated_at`: Timestamps

### Data Classes
The application also defines several `dataclass` structures for handling job parameters (not stored directly as separate tables, likely serialized or used for validation):
- `AlignmentParams`
- `TrimmingParams`
- `TreeBuilderParams`
- `JobParams`

## Application Structure (`app/`)
The application follows a modular factory pattern. Below is a detailed breakdown of the file structure:

### 1. Configuration & Core
- `app/__init__.py`: **Application Factory**. Initializes the Flask app, registers extensions and blueprints.
- `app/config.py`: Configuration classes (Development, Production) loading settings from environment variables.
- `app/extensions.py`: Initialization of Flask extensions (`db`, `login_manager`, `migrate`, `redis_client`).
- `app/cli.py`: Custom Flask CLI commands (e.g., `flask run-worker`, `flask run-metrics`).
- `app/models.py`: Database models (`User`, `Job`) and data classes (`AlignmentParams`, etc.).

### 2. Blueprints (Route Logic)
Each blueprint encapsulates a domain of the application:
- **`app/main/`**: Core application logic.
    - `routes.py`: General routes (landing page, static pages).
- **`app/auth/`**: Authentication.
    - `routes.py`: Login, logout, registration flows.
- **`app/user/`**: User-centric views.
    - `routes.py`: User dashboard, job list (`/user/jobs`).
- **`app/api/`**: REST API for frontend interactions.
    - `routes.py`: Endpoints for job status, data download, and tree operations.
- **`app/monitoring/`**: System health.
    - `services.py`: Logic for collecting system metrics (`psutil`).
    - `routes.py`: Dashboard for viewing system load.

### 3. Services (Business Logic)
Encapsulates complex logic, separated from route handlers:
- `app/services/alignment_service.py`: Handles sequence alignment (MAFFT, MUSCLE, etc.).
- `app/services/tree_builder_service.py`: Handles phylogenetic tree inference (RAxML, IQ-TREE).
- `app/services/trimming_service.py`: Sequence trimming (trimAl).
- `app/services/tree_edit_service.py`: Tree manipulation logic (rerooting, pruning, node renaming).
- `app/services/blast_service.py`: Integration with BLAST tools.
- `app/services/subprocess_utils.py`: Utilities for safely execution shell commands.

### 4. Background Workers (`app/workers/`)
Handles asynchronous task processing using Redis Queue (RQ):
- `tasks.py`: Entry points for background jobs (e.g., `run_alignment`, `run_tree_building`).
- `queue.py`: Helper functions to enqueue jobs.
- `worker_monitor.py`: Logic to monitor worker health and heartbeat.

### 5. Frontend (`app/templates/` & `app/static/`)
#### Templates (Jinja2)
- **Base Layouts**:
    - `base.html`: Main layout wrapper.
    - `index.html`: Home page.
- **Job Views**:
    - `user_jobs.html`: List of user's submitted jobs.
    - `job_viewer.html`: Detailed view of a specific job (results).
    - `job_status.html`: Current status of a running job.
- **Partials**:
    - `partials/viewer_controls.html`: Control panel for tree viewers.
    - `partials/phylotree_viewer.html`: Container for Phylotree.js.
- **Admin**:
    - `admin/monitoring.html`: System metrics dashboard.

#### Static Assets
- **CSS** (`app/static/css/`):
    - `style.css`: Global styles.
    - `tree_viewer.css`: Specific styles for the tree visualization.
- **JavaScript** (`app/static/js/`):
    - `tree_viewer_controller.js`: **Main Controller**. Orchestrates the tree viewer UI.
    - `tree_viewer_api.js`: Handles AJAX requests to module API.
    - `tree_edit_actions.js`: Bridges UI actions to API calls (prune, reroot).
    - `phylotree.js` / `tree_viewer_phylotree_v2.js`: D3-based tree rendering logic.

## Tree state concurrency (`tree_state.json`)

Every viewer edit — prune, rename, reroot, selection sets, clade annotations —
is a read/modify/write of one whole JSON document, and several of them can be in
flight at once (two tabs, the worker's post-completion hooks, a recompute).
`tree_state_lock(job_dir)` in `tree_edit_service.py` serializes them: an
`flock` on a separate `.tree_state.lock` file, so it survives the atomic replace
of `tree_state.json` and coordinates across Gunicorn processes. It is re-entrant
per thread, so a nested `save_tree_state()` inside a locked block is safe.

Two rules keep it useful:

1. **Load and save inside the same lock.** A load outside the lock produces a
   snapshot that another writer can overtake; saving it later silently reverts
   their edit. Read-only paths (the state GET, the FASTA downloads) need no lock.
2. **Never hold the lock across slow work** — no HTTP, no MAFFT/RAxML, no
   subprocess. Long operations use *reload-at-commit* instead: do the expensive
   work unlocked, then take the lock, reload the latest state, merge in only what
   the operation owns, and save.

`commit_recompute_tree_state()` is the reference implementation of rule 2.
Recompute owns `tree_structure`, `current_tree` and the rooting reapplied to the
new topology; renames, pruned taxa, selection sets and their colours,
`sequence_of_interest`, annotation layers and annotations all come from the
latest state, and annotations are reconciled against the new tree with
`restrict_annotations_to_current_leaves()` (members whose tips vanished are
dropped; one-member annotations and annotations that no longer form a clade are
kept and flagged by the viewer). The MycoMap record refresh and the
iNaturalist/Mushroom Observer source-tip highlighters follow the same pattern:
the remote lookup happens first, the state is read and written under the lock
afterwards.

## Claude Review of a Finished Tree

`app/services/tree_analysis_service.py` backs the tree viewer's **Analyze with
Claude** button (`POST /api/job/<id>/analysis/review`). It answers one question
for the user: is this tree worth trusting?

The design point is that **the model never sees the sequences**. Every number —
gap fraction, column occupancy, parsimony-informative count, mean pairwise
identity, support distribution, long-branch outliers — is computed here from
`alignment/alignment_trimmed.fasta` and the current Newick, and only that
summary (median 27 KB, max 30 KB over a 60-job sample) goes to the API. The
summary is bounded by construction — every list in it is capped at TOP_N — so
prompt size does not grow with alignment size. Counting is work
code does exactly and a language model does slowly; the model is used only for
the judgement of reading those numbers together.

Three consequences follow:

- **Response time is independent of alignment size.** Metric assembly is ~2 s on
  the largest job in `var/jobs` (2400 sequences x 3300 columns); everything after
  that is model latency. Alignments past ~12M cells switch to evenly spaced
  column sampling, reported as `column_sampling_applied`.
- **The alignment is restricted to the tips currently in the tree**, so a review
  of a pruned tree does not report statistics for sequences the user removed.
  Viewer renames are passed through so the review names what is on screen.
- **Reviews are cached on a fingerprint of the metrics**, at
  `var/jobs/<id>/analysis/claude_review.json`. Re-opening the viewer replays the
  stored review; only a real change to the tree or alignment, or an explicit
  Re-run, costs a call.

Support values are classified with the same rules as the viewer's support badge
(`tree_viewer_phylotree_v2.js`), including resolving IQ-TREE's dual
`SH-aLRT/UFBoot` labels, so the review and the badge can never disagree about a
node. Both sides read the *same resolved builder*: `resolve_tree_support_context()`
is what fills `window.TREE_METHOD`, so a recomputed job whose displayed tree was
rebuilt by a different builder cannot classify one way on the badge and another
in the review. The classification is **method-first**: the builder that wrote the file
decides the scale, and the magnitude of the values is consulted only for a
builder neither side recognises. RAxML-NG writes bootstrap values of `0`, `1`
and `0.95` for badly supported clades, and the older "everything ≤ 1 must be a
posterior" rule relabelled those trees as Bayesian. The scale's meaning is sent
with the numbers — FastTree's 0–1 SH-like support in particular is neither a
bootstrap proportion nor a posterior probability, and is routinely misread as
one.

The two implementations are pinned to one another by
`tests/fixtures/support_classification_cases.json`.
`tests/test_tree_analysis_metrics.py` runs every case against `_classify_support()`
in Python and, under `node`, against `window.classifySupportType()` from the
viewer file, so the pair cannot drift apart silently. Add a case there rather
than to either implementation alone.

### Metric contracts worth knowing

- **Column tallies are exact only when the whole alignment was scored.** Past
  `MAX_ALIGNMENT_CELLS` the columns are sampled, and the bare counts
  (`parsimony_informative_columns`, `columns_below_50_percent_occupancy`,
  `all_gap_columns`) are then *absent* — replaced by `*_estimated` siblings
  scaled from the sample, with `column_metrics_are_estimates: true` covering the
  lot. A name without the suffix therefore always means an exact count. The
  viewer's facts strip follows the same rule and labels an estimate as one.
- **Occupancy is non-gap occupancy.** An ambiguous base such as `N` occupies its
  column but contributes no state to the parsimony-informative test, so 100%
  occupancy is not 100% confidently called bases. The ambiguity fields carry
  that separately, and `alignment.occupancy_definition` says so in the prompt.
- **Rooting comes from `tree_state.json`, not from topology.** Dikarya
  midpoint-roots by default, so neither a null `outgroup` nor a root of degree 3
  means the tree is unrooted. `tree.rooting` reports `root_mode`, `root_target`
  and `is_midpoint_rooted`. Two different negatives are kept apart:
  `state_known: false` means the file could not be read, and
  `rooting_known: false` means it was read but carries no rooting information --
  which every rename and prune produces, and which the old code reported as
  *explicitly unrooted*. Only an explicit mode, or an explicit
  `is_midpoint_rooted: true`, licenses a statement about the rooting. The root is
  likewise never counted as a polytomy, and the Newick root's degree is reported
  separately as `file_root_degree` (renamed from `root_degree`, which read as the
  viewer's rooting state).
- **Internal nodes are informative splits, not Newick nodes.** An unrooted binary
  tree written through a rooted Newick root has an artificial root whose two
  children are the two sides of one bipartition. They are merged into a single
  edge, with the two half-lengths summed and the one support label that either
  side carries -- previously they were two nodes, one of them a phantom
  unsupported one, and the edge appeared in the quantiles at half its length.
  `artificial_root_edge_merged` says whether that happened.
- **Long-branch outliers are cut from the positive terminal branches only.** The
  rule is `max(Q3 + 3*IQR, 5*median)` over those, published verbatim as
  `outlier_rule`. On a tree dominated by zero-length tips -- the common shape
  here -- the old quartiles were both zero, the cut collapsed to zero, and a
  40x branch was reported as no outlier at all; the 5x-median floor stops the
  opposite failure, where a tight cluster of tiny lengths makes every slightly
  longer branch a false suspect. Quantiles and branch lengths below 1e-4 keep
  three significant figures rather than rounding to six decimals, for the same
  reason `write_tree_file()` exists.
- **The alignment says what it is.** `alignment_is_tree_builder_input`,
  `alignment_is_trim_output` and `alignment_restricted_to_current_tips` are
  reported rather than assumed. After a recompute the displayed tree came from
  `alignment_pruned_trimmed.fasta`, not from `alignment_trimmed.fasta`, and
  `alignment_pruned_aligned.fasta` is realigned rather than trimmed --
  `columns_removed_by_trimming` is published only for a genuine trim pair (named
  in `trimming_measured_from`) with `trimming_method != none`. When the row set
  was restricted to the displayed tips, `scope_note` says so and says that
  support was estimated on the full builder alignment.
- **IQ-TREE has three support scales, not one.** `UFBOOT` (ultrafast bootstrap,
  strong at 95 and with no conventional moderate band -- the classical 70 does
  not apply), `ALRT` (SH-aLRT only, when `-alrt` ran without `-B`, strong at 80)
  and `ALRT_UFBOOT` (the dual label). `moderate_support_threshold` is null for
  scales with no middle band, and no `at_least_moderate_percent` is published for
  them.
- **Support is stratified by the branch it sits on.**
  `support_by_subtending_branch_length` splits scored splits at 1e-6, so weak
  support inside a cluster of near-identical sequences is distinguishable from a
  weakly supported backbone without the model doing the partition itself.
- **A missing branch length is not a zero one.** `zero_length_terminal_branches`
  counts tips whose length is present and non-positive;
  `tips_missing_branch_length` counts tips with no length at all, and those are
  excluded from the quantiles, the total, the outlier cut and the longest-tip
  ranking rather than entered as zeros.
- **Truncated lists carry their totals.** `outlier_tip_count`,
  `identical_sequence_group_count` and `sequences_in_identical_groups_total` are
  computed before the `TOP_N` slice, and each identical-sequence group keeps its
  own `count` plus a `names_truncated` flag.
- **`_validate_review()` enforces the contract, not just key presence.** Ratings
  and severities must be in their `RESPONSE_SCHEMA` enums and every field must
  have its declared type before a reply is cached or rendered. It also enforces
  the 140-character headline, that every `sequences_to_inspect[].name` resolves
  to a tip the viewer is currently showing, and the two rating/severity
  combinations the prompt rules out (`strong` with a high-severity concern,
  `unreliable` with none). A failure here raises `TreeAnalysisUpstreamError` and
  the endpoint answers **502**: the browser's request was fine, the model's reply
  was not. A dataset with nothing to review is still 400. The viewer's
  fallback for an unexpected rating is a neutral grey *Unrated* chip — never the
  "Usable" styling, which would dress a malformed answer up as a favourable one.

### Backends

`CLAUDE_REVIEW_BACKEND` selects how the call is made. Both produce the same
schema-validated review; only the transport differs.

**`cli` (default)** — shells out to the `tree` account's Claude Code CLI. No API
key. The web app runs as `dikarya` and `tree`'s credentials are mode 600, so it
goes through a root-owned sudo wrapper.

The wrapper (`scripts/dikarya-claude-review`) **takes no arguments at all**. It
is called by a process handling untrusted internet input, so every `claude` flag
is pinned inside a file `dikarya` cannot edit — forwarding `"$@"` would hand that
process `--tools Bash` or `--dangerously-skip-permissions`, which is arbitrary
code execution as `tree`. Same reasoning as `dikarya-journal` not forwarding
arguments to `journalctl`. The prompt arrives on stdin, where it can only be
data; the sudoers entry ends in `""` so sudo permits no arguments either.

The pinned invocation is `--tools ""` (no tools — this is one inference call, not
an agent), `--safe-mode` (no CLAUDE.md, skills, plugins, hooks, MCP),
`--strict-mcp-config`, `--disable-slash-commands`, `--no-session-persistence`,
plus `--system-prompt` and `--json-schema` read from root-owned files in
`/etc/dikarya-claude-review/`, and `--max-budget-usd` as a hard spend stop. It
runs from an empty scratch directory so there is no project config to discover
in the first place. Model, effort, timeout and budget arrive as environment
variables and are checked against allowlists before reaching the CLI.

**`api`** — calls the Anthropic API directly with `ANTHROPIC_API_KEY`, using
streaming, adaptive thinking, and `output_config.format` for the same schema.
Faster and cheaper per review; needs a key.

Measured on a 147-sequence job, `cli` backend, Claude Opus 5:

| Effort | Wall clock | Cost | Verdict |
|---|---|---|---|
| `low` (default) | 69–136 s | $0.24–$0.48 | same |
| `medium` | ~200 s | ~$0.39 | same |

Measured end-to-end through the installed wrapper. The spread tracks prompt
size; the high end of each row is a ~27 KB prompt, at or above the largest seen
in a 500-job sample. `medium` at that size leaves only ~40 s under the wrapper's
240 s cap, which is why `low` is the default and why the wrapper's own fallback
was changed to match it — if `env_keep` ever stops propagating
`DIKARYA_CLAUDE_EFFORT`, the fallback must be the path that fits.

Gunicorn's `--timeout 120` does **not** abort these. That timeout is a worker
heartbeat, and the `gthread` worker keeps notifying the arbiter while a request
occupies a thread — `POST /tree/recompute` has returned 200 after 1751 s in this
deployment. The binding limits are the ones in the chain above.

`low` is the default because the request is synchronous and nginx's
`proxy_read_timeout` for `location /` is 300 s. The CLI also carries ~17–28 K
tokens of Claude Code harness context per invocation, cached after the first
call — this is why the `api` backend is cheaper if a key is available.

**Usage accounting.** Every *billed* review appends one JSON line to
`var/logs/claude_reviews.jsonl` (`_append_usage_log()`); cache hits never reach
it, so the totals are what the feature actually consumed rather than how often
the button was pressed. `/admin/monitoring` aggregates the last 30 days from it
via `get_ai_usage()` in `app/monitoring/services.py`. Writing the log is
best-effort and every failure is swallowed — a monitoring line must never cost a
user their review.

A Redis counter enforces a site-wide allowance of 25 new reviews per UTC day;
cache hits do not consume it. The dated key is seeded from the usage log when it
is first created, so deploying the guard partway through a day or restarting
Redis does not reset completed usage. Reservations are not refunded after a
model failure because a timeout or unusable reply may still have incurred cost.
If Redis cannot verify the counter, new reviews fail closed while cached reviews
remain available.

`scripts/dikarya_backfill_review_usage.py` replays reviews already cached on
disk into the log, keyed on `(job_id, ts)` so it is safe to re-run. It writes to
`var/logs`, which is dikarya-owned, so agents cannot run it:

```bash
sudo -u dikarya /var/www/dikarya/.venv/bin/python \
    /var/www/dikarya/scripts/dikarya_backfill_review_usage.py --dry-run
```

Note `/admin/monitoring` is unauthenticated, so these usage totals are public.
They expose no job or user data, but they do reveal how much the feature is
being used.

**Configuration** (unconfigured → button not rendered, endpoint answers 503):

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_REVIEW_BACKEND` | `cli` | `cli` or `api` |
| `CLAUDE_REVIEW_WRAPPER` | `/usr/local/sbin/dikarya-claude-review` | Sudo wrapper path (`cli`) |
| `ANTHROPIC_API_KEY` | — | Required by `api` only |
| `CLAUDE_REVIEW_MODEL` | `claude-opus-5` | Model id |
| `CLAUDE_REVIEW_EFFORT` | `low` | `low`–`max` |
| `CLAUDE_REVIEW_MAX_TOKENS` | `32000` | Thinking + output ceiling (`api`) |
| `CLAUDE_REVIEW_MAX_BUDGET_USD` | `1.00` | Per-invocation spend stop (`cli`); capped at `2.00`, see below |
| `CLAUDE_REVIEW_TIMEOUT_SECONDS` | `240` | Hard cap; must stay under nginx's 300 s |
| `CLAUDE_REVIEW_MAX_CONCURRENT` | `2` | Global in-flight ceiling |
| `CLAUDE_REVIEW_MAX_DAILY` | `25` | Site-wide new reviews per UTC day; cache hits excluded |

#### The `$2.00` budget hard cap

`CLAUDE_REVIEW_MAX_BUDGET_USD` is configurable, but not without limit. The value
reaches the CLI through an environment variable that an internet-facing process
sets, so if it could be set to any magnitude then `--max-budget-usd` — the one
control that stops a runaway review from billing without bound — could simply be
configured away. Both layers therefore refuse anything above **$2.00**:

* `CLAUDE_REVIEW_MAX_BUDGET_HARD_CAP_USD` in `app/config.py` (unprivileged;
  canonicalizes the value and falls back to the default with a logged warning),
* `MAX_BUDGET_HARD_CENTS` in `scripts/dikarya-claude-review` (privileged; the
  actual enforcement boundary, which exits 64 rather than falling back).

The two must stay equal; `tests/test_maintenance_script_safety.py` asserts it.

$2.00 is derived rather than chosen for roundness:

* twice the shipped default of `1.00`, so the budget can still be raised
  deliberately for a higher effort setting or an unusually large tree;
* roughly six times the measured cost of a real review — low effort $0.25,
  medium $0.35 on a 147-sequence job;
* and it bounds worst-case site-wide spend at `CLAUDE_REVIEW_MAX_DAILY` × $2.00
  = **$50 per UTC day**, which is the number to re-derive if either value moves.

The two layers do not implement the same grammar and are not meant to. The app
canonicalizes — it tolerates surrounding whitespace and any accepted precision,
and always emits the two-decimal form — while the wrapper accepts only that
canonical form. The invariant is one-directional: everything the app can emit,
the wrapper accepts, so a legal configuration can never fail inside `sudo`.

### Installing the `cli` backend (root)

```bash
install -o root -g root -m 0755 \
    /var/www/dikarya/scripts/dikarya-claude-review /usr/local/sbin/
install -o root -g root -m 0440 \
    /var/www/dikarya/ops/sudoers/dikarya-claude /etc/sudoers.d/dikarya-claude
visudo -c

mkdir -p /etc/dikarya-claude-review
chmod 0755 /etc/dikarya-claude-review
cd /var/www/dikarya
.venv/bin/python scripts/dikarya_export_review_prompt.py system \
    > /etc/dikarya-claude-review/system_prompt.txt
.venv/bin/python scripts/dikarya_export_review_prompt.py schema \
    > /etc/dikarya-claude-review/schema.json
chmod 0444 /etc/dikarya-claude-review/*

sudo /usr/local/sbin/restart-dikarya-web
```

The system prompt and schema live in `/etc` rather than the repo so the web
process cannot rewrite the reviewer's own instructions. They sit in their own
top-level directory rather than under `/etc/dikarya/`, because that directory is
`drwx------ root root` — it holds `dikarya.environment.live` — and the wrapper
runs as `tree`, which cannot traverse it. Loosening the mode on the secrets
directory to fix that would be the wrong trade; a separate directory costs
nothing and leaves it untouched. That makes them a copy,
and copies go stale — `dikarya_export_review_prompt.py --check` compares them
against the code and exits non-zero on drift. Run it after any change to
`SYSTEM_PROMPT` or `RESPONSE_SCHEMA`.

The review runs inside a Gunicorn request slot, and there are only
`4 workers x 2 threads = 8` of them. Flask-Limiter caps one client but cannot
stop eight different users from taking every slot, so Redis enforces a global
ceiling and returns 503 past it. The registry (`dikarya:claude_review:in_flight`)
is a **sorted set of request tokens scored by acquisition time**, not a counter:
entries older than the worst-case transport time plus a 30 s grace are dropped
before the ceiling is checked, so a worker killed mid-review leaks its slot for
one timeout
rather than until a shared expiry, a *rejected* request can no longer postpone
that expiry by touching the key, and there is no counter to drive negative when a
release races an expiry.

A second guard stops the same review being bought twice.
`dikarya:claude_review:lock:<fingerprint>` is taken with `SET NX` before the slot
and before the daily reservation, and released with a delete-if-owned script, so
a double click, a reload mid-call, or two people on a shared link produce one
Claude call and one charge; the duplicate gets **409** with `Retry-After`.

Both lifetimes come from `_max_transport_seconds()`, which is the timeout for the
CLI backend and `(CLAUDE_API_MAX_RETRIES + 1) x` the timeout for the API one. The
Anthropic SDK applies its timeout **per attempt**, so an API review that retries a
529 can legitimately run for two full timeouts; a lifetime of one timeout expired
underneath it and let the next request take the same fingerprint lock and buy the
same review twice -- the one thing this guard exists to prevent. Both
guards degrade to "unlimited" if Redis is unreachable, matching the existing
fail-safe intent -- only the daily spending guard fails closed.

## Runtime & Deployment
- **Environment**: Remote Linux Server
- **Virtual Environment**: `/var/www/dikarya/.venv`
- **Entry Point**: `wsgi.py`
- **System Services (Systemd)**:
    - `dikarya-web.service`: The web application server
    - `dikarya-worker.service`: Asynchronous task worker
    - **Note**: Service runs on `https://127.0.0.1:8000`

## Development Workflow

### Web Application
To run the app in development mode with hot-reloading:

```bash
export FLASK_APP=wsgi.py
export FLASK_ENV=development
flask run --port=5000
```

### Worker
To run the worker in development mode:

```bash
rq worker -u redis://localhost:6379 dikarya-tasks
```

## Debugging & Logging

### Logs
- **Error Log**: `/var/www/dikarya/var/logs/error.log`
- **Access Log**: `/var/www/dikarya/var/logs/access.log`

### Troubleshooting
- **500 Server Errors**: Check the error log for stack traces.
- **Route Changes**: If you add or modify routes (e.g., adding `user.clear_jobs`), you **MUST** reload the Gunicorn server for changes to take effect.
    - Find the Master PID: `ps aux | grep gunicorn`
    - Send HUP signal: `kill -HUP <MASTER_PID>`
