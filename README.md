# Dikarya

Dikarya is a Flask web application for fungal phylogenetic analysis. It accepts
FASTA sequences and GenBank accessions, runs configurable alignment, trimming,
and tree-building pipelines in Redis Queue (RQ) workers, streams job progress
with Server-Sent Events, and displays completed trees in an interactive viewer.

The application is designed for Python 3.12 and has been developed and locked
with CPython 3.12.3 on Linux.

## Architecture

- Flask and Gunicorn serve the web application and JSON APIs.
- PostgreSQL stores users, jobs, API tokens, and application metadata.
- Redis provides RQ job queues, job-event Pub/Sub, rate-limit storage, and the
  RQ scheduler.
- One or more RQ workers execute the bioinformatics pipeline.
- Job inputs, outputs, trees, and logs are stored below `JOB_DIR` (by default,
  `var/jobs`).

The pipeline is `INPUT -> ORIENT -> BLAST -> ALIGN -> TRIM -> TREE -> POST`.
Orientation, remote BLAST, and trimming are optional.

## Requirements

Install these services before running Dikarya:

- Python 3.12
- PostgreSQL
- Redis

Dikarya invokes external bioinformatics programs directly. Install the
programs needed by the pipeline choices you intend to offer and set their
paths in `.env` when they are not on `PATH`.

| Program | Dikarya use | Environment variable |
| --- | --- | --- |
| MAFFT | Default multiple-sequence alignment | `MAFFT_BINARY` |
| MUSCLE 5 | Optional alignment | `MUSCLE_BINARY` |
| Clustal Omega | Optional alignment | `CLUSTALO_BINARY` |
| IQ-TREE 2 | Optional alignment/tree workflows | `IQTREE_BINARY` |
| RAxML-NG | Maximum-likelihood tree building | `RAXML_BINARY` |
| MrBayes | Bayesian tree building | `MRBAYES_BINARY` |
| FastTree | Quick tree building | `FASTTREE_BINARY` |
| trimAl | Optional alignment trimming | `TRIMAL_BINARY` |
| BMGE | Optional alignment trimming; a runnable wrapper may be used | `BMGE_BINARY` |

Only the tools selected for a job are invoked. The one-click tree flows use
MAFFT and FastTree by default, so those two programs are the practical minimum
for that workflow. NCBI BLAST is accessed remotely and does not require a local
BLAST+ installation.

## Installation

Clone the repository, create a Python 3.12 virtual environment, and install the
pinned production dependency set:

```bash
git clone https://github.com/AlanRockefeller/dikarya-phylogenetic-tree-generator dikarya
cd dikarya
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

For development, install the production dependencies plus the pinned test,
dependency-audit, secret-scan, and lockfile tools:

```bash
python -m pip install -r requirements-dev.txt
```

The lock files target CPython 3.12 on Linux. Regenerate and verify them on a
different Python version or operating system before treating that environment
as supported.

## Configuration

Copy the example environment file and replace every placeholder relevant to
your deployment:

```bash
cp .env.example .env
```

At minimum, set a strong `SECRET_KEY`, `DATABASE_URL`, and `REDIS_URL`. Generate
a production secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Do not commit `.env`, OAuth tokens, API keys, databases, or files below `var/`.
Production starts refuse to use Dikarya's known development secret.

Create the PostgreSQL database and role using your normal PostgreSQL
administration workflow, then put the resulting SQLAlchemy URL in
`DATABASE_URL`. Redis must be reachable at `REDIS_URL` by both the web process
and every worker.

## Database migrations

Apply all committed migrations before starting a new deployment or after
updating the application:

```bash
source .venv/bin/activate
export FLASK_APP=wsgi.py
flask db upgrade
```

Create migration revisions only when intentionally changing SQLAlchemy models:

```bash
flask db migrate -m "describe the schema change"
```

Review generated migrations before applying or committing them.

## Running locally

Start Redis and PostgreSQL, set `FLASK_ENV=development` in `.env`, and run the
web process:

```bash
source .venv/bin/activate
flask run --port=5000
```

In a second shell, start the worker. Jobs will remain queued if no worker is
running:

```bash
source .venv/bin/activate
flask run-worker
```

The worker listens to the `phylo_high` and `phylo_bulk` queues and starts the
RQ scheduler. The optional metrics collector is:

```bash
flask run-metrics
```

## Running with Gunicorn

For a production-style web process behind a TLS-terminating reverse proxy:

```bash
source .venv/bin/activate
export FLASK_ENV=production
gunicorn --bind 127.0.0.1:5000 wsgi:app
```

Run `flask run-worker` as a separate supervised service. A production
deployment should also provide persistent PostgreSQL and Redis services,
writable `JOB_DIR`, `WORKER_DIR`, and `METRICS_FILE` locations, log rotation,
database and job-data backups, and an HTTPS reverse proxy. Run migrations as a
deployment step rather than from every Gunicorn worker.

## Optional integrations and data

### MycoMap

Importing sequences from an existing public MycoMap BLAST-result URL does not
require an API credential. Creating a MycoMap BLAST from an iNaturalist ITS
sequence, or rerunning local/NCBI MycoMap searches, requires
`MYCOMAP_COM_API_KEY`. `MYCOMAP_COM_USER_ID` selects the MycoMap account that
owns API-created searches. Hit limits, the legacy NCBI grace period, and the
polling interval/attempt limit are configurable in `.env.example`.

Keep the MycoMap API key server-side. Never expose it in browser JavaScript,
logs, commits, or screenshots.

### iNaturalist

Reading public observations, projects, users, and DNA fields uses the public
iNaturalist API and does not require OAuth. Connecting a site-wide iNaturalist
account and writing phylogenetic-tree links back to observations requires an
iNaturalist OAuth application and the `INAT_*` settings in `.env.example`.
Restrict OAuth controls with `INAT_OAUTH_ADMIN_EMAILS`, and keep the token file
under a private, persistent path.

### NCBI

GenBank retrieval and BLAST use NCBI's remote services. Set `BLAST_EMAIL` to a
monitored contact address so requests identify the deployment. NCBI-dependent
features require outbound HTTPS access and remain subject to NCBI availability
and usage policies.

## Tests and security checks

Run the complete test suite with:

```bash
python -m pytest
```

Audit the pinned production dependencies and scan tracked files for likely
secrets with:

```bash
python -m pip_audit -r requirements.txt
git ls-files -z | xargs -0 detect-secrets-hook
```

`detect-secrets` findings require human review; a clean scan is not proof that
the repository has never contained a credential. Scan Git history separately
before the first public push.

The direct dependency declarations are in `requirements.in` and
`requirements-dev.in`. After intentionally changing them, regenerate the locks
under Python 3.12:

```bash
python -m piptools compile --strip-extras --output-file requirements.txt requirements.in
python -m piptools compile --strip-extras --allow-unsafe --output-file requirements-dev.txt requirements-dev.in
```

Then reinstall in a clean virtual environment, run the tests and audit, and
review the lockfile diff before committing it.

## Security

Please report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
Do not publish suspected vulnerabilities or proof-of-concept exploits in a
public issue before a fix is available.

## Credits

Voucher Sync (`/voucher-sync`) was contributed by Bryce Thorson, ported from the
standalone desktop application
[inat-voucher-sync](https://github.com/bthorson1029/inat-voucher-sync) (MIT,
[doi:10.5281/zenodo.22064695](https://doi.org/10.5281/zenodo.22064695)), which
is still maintained separately.

## License

Dikarya is released under the [MIT License](LICENSE.md).
Vendored browser libraries, styles, and fonts are documented in the
[third-party notices](app/static/vendor/THIRD_PARTY_NOTICES.md).
