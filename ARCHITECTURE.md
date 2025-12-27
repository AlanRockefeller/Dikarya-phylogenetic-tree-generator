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
The application follows a modular factory pattern:

- **Blueprints**:
    - `api/`: REST API endpoints
    - `auth/`: Authentication routes
    - `main/`: Core application logic/UI
    - `user/`: User profile management
    - `monitoring/`: System and job monitoring
- **Core Components**:
    - `__init__.py`: Application factory (`create_app`) and blueprint registration.
    - `models.py`: SQLAlchemy models and dataclasses.
    - `workers/`: RQ worker configurations.
    - `services/`: Business logic isolation.
    - `extensions.py`: Flask extension initialization (db, login_manager, migrate).

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
