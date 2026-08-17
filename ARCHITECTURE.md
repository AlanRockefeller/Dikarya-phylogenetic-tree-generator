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
summary (8–17 KB of JSON on the real corpus) goes to the API. Counting is work
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
node. The scale's meaning is sent with the numbers — FastTree's 0–1 SH-like
support in particular is neither a bootstrap proportion nor a posterior
probability, and is routinely misread as one.

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
`/etc/dikarya/claude-review/`, and `--max-budget-usd` as a hard spend stop. It
runs from an empty scratch directory so there is no project config to discover
in the first place. Model, effort, timeout and budget arrive as environment
variables and are checked against allowlists before reaching the CLI.

**`api`** — calls the Anthropic API directly with `ANTHROPIC_API_KEY`, using
streaming, adaptive thinking, and `output_config.format` for the same schema.
Faster and cheaper per review; needs a key.

Measured on a 147-sequence job, `cli` backend, Claude Opus 5:

| Effort | Wall clock | Cost | Verdict |
|---|---|---|---|
| `low` (default) | ~60–90 s | ~$0.24 | same |
| `medium` | ~156 s | ~$0.35 | same |

`low` is the default because the request is synchronous and nginx's
`proxy_read_timeout` for `location /` is 300 s. The CLI also carries ~17–28 K
tokens of Claude Code harness context per invocation, cached after the first
call — this is why the `api` backend is cheaper if a key is available.

**Configuration** (unconfigured → button not rendered, endpoint answers 503):

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_REVIEW_BACKEND` | `cli` | `cli` or `api` |
| `CLAUDE_REVIEW_WRAPPER` | `/usr/local/sbin/dikarya-claude-review` | Sudo wrapper path (`cli`) |
| `ANTHROPIC_API_KEY` | — | Required by `api` only |
| `CLAUDE_REVIEW_MODEL` | `claude-opus-5` | Model id |
| `CLAUDE_REVIEW_EFFORT` | `low` | `low`–`max` |
| `CLAUDE_REVIEW_MAX_TOKENS` | `32000` | Thinking + output ceiling (`api`) |
| `CLAUDE_REVIEW_MAX_BUDGET_USD` | `1.00` | Per-invocation spend stop (`cli`) |
| `CLAUDE_REVIEW_TIMEOUT_SECONDS` | `240` | Hard cap; must stay under nginx's 300 s |
| `CLAUDE_REVIEW_MAX_CONCURRENT` | `2` | Global in-flight ceiling |

### Installing the `cli` backend (root)

```bash
install -o root -g root -m 0755 \
    /var/www/dikarya/scripts/dikarya-claude-review /usr/local/sbin/
install -o root -g root -m 0440 \
    /var/www/dikarya/ops/sudoers/dikarya-claude /etc/sudoers.d/dikarya-claude
visudo -c

mkdir -p /etc/dikarya/claude-review
cd /var/www/dikarya
.venv/bin/python scripts/dikarya_export_review_prompt.py system \
    > /etc/dikarya/claude-review/system_prompt.txt
.venv/bin/python scripts/dikarya_export_review_prompt.py schema \
    > /etc/dikarya/claude-review/schema.json
chmod 0444 /etc/dikarya/claude-review/*

sudo /usr/local/sbin/restart-dikarya-web
```

The system prompt and schema live in `/etc` rather than the repo so the web
process cannot rewrite the reviewer's own instructions. That makes them a copy,
and copies go stale — `dikarya_export_review_prompt.py --check` compares them
against the code and exits non-zero on drift. Run it after any change to
`SYSTEM_PROMPT` or `RESPONSE_SCHEMA`.

The review runs inside a Gunicorn request slot, and there are only
`4 workers x 2 threads = 8` of them. Flask-Limiter caps one client but cannot
stop eight different users from taking every slot, so a Redis counter
(`dikarya:claude_review:in_flight`) enforces a global ceiling and returns 503
past it. That counter has a 900 s expiry so a killed worker cannot leak a slot
permanently.

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
