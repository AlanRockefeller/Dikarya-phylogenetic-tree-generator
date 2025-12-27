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
