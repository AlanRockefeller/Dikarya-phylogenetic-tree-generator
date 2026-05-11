"""Hand-curated OpenAPI 3.1 spec for /api/v1.

Kept as a Python dict so we can interpolate the deployed host at request
time and emit either JSON or YAML. Update this file when adding endpoints.
"""
from flask import request


def _schemas():
    return {
        "Error": {
            "type": "object",
            "required": ["error"],
            "properties": {
                "error": {
                    "type": "object",
                    "required": ["code", "message", "request_id"],
                    "properties": {
                        "code": {"type": "string", "example": "validation_failed"},
                        "message": {"type": "string"},
                        "request_id": {"type": "string", "example": "a1b2c3d4e5f6"},
                        "details": {"type": "object"},
                    },
                }
            },
        },
        "Job": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "format": "uuid"},
                "status": {"type": "string", "enum": ["queued", "running", "completed", "failed", "error"]},
                "created_at": {"type": "string", "format": "date-time"},
                "updated_at": {"type": "string", "format": "date-time"},
                "input_type": {"type": "string"},
                "notes": {"type": "string"},
                "params": {
                    "type": "object",
                    "properties": {
                        "alignment_method": {"type": "string"},
                        "trimming_method": {"type": "string"},
                        "tree_method": {"type": "string"},
                        "tree_model": {"type": "string"},
                        "bootstrap": {"type": "integer"},
                        "mcmc_generations": {"type": "integer"},
                    },
                },
                "metrics": {"type": "object"},
                "links": {
                    "type": "object",
                    "properties": {
                        "self": {"type": "string"},
                        "events": {"type": "string"},
                        "files": {"type": "string"},
                        "view": {"type": "string"},
                    },
                },
            },
        },
        "CreateJobRequest": {
            "type": "object",
            "required": ["tree_method"],
            "properties": {
                "input_type": {"type": "string", "default": "fasta"},
                "sequence": {"type": "string", "description": "FASTA-formatted sequence text. Max 5 MB."},
                "accessions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of GenBank accession numbers. Max 500.",
                },
                "alignment_method": {
                    "type": "string",
                    "enum": ["mafft", "muscle", "clustalo", "iqtree_builtin", "default"],
                    "default": "mafft",
                },
                "trimming_method": {
                    "type": "string",
                    "enum": ["none", "trimal", "bmge"],
                    "default": "none",
                },
                "tree_method": {
                    "type": "string",
                    "enum": ["nj", "raxml", "iqtree", "mrbayes", "fasttree"],
                },
                "tree_model": {"type": "string", "default": "GTR+G"},
                "bootstrap": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 1000},
                "mcmc_generations": {"type": "integer", "minimum": 1000, "maximum": 100000000, "default": 50000},
                "mcmc_nruns": {"type": "integer", "minimum": 1, "maximum": 8, "default": 2},
                "mcmc_nchains": {"type": "integer", "minimum": 1, "maximum": 16, "default": 4},
                "notes": {"type": "string", "maxLength": 2000},
            },
        },
        "Artifact": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "size_bytes": {"type": "integer"},
                "mime": {"type": "string"},
                "url": {"type": "string"},
            },
        },
        "Token": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "name": {"type": "string"},
                "prefix": {"type": "string"},
                "scopes": {"type": "array", "items": {"type": "string"}},
                "created_at": {"type": "string", "format": "date-time"},
                "last_used_at": {"type": "string", "format": "date-time", "nullable": True},
                "revoked_at": {"type": "string", "format": "date-time", "nullable": True},
            },
        },
        "User": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "email": {"type": "string", "format": "email"},
                "created_at": {"type": "string", "format": "date-time"},
            },
        },
    }


def _data_response(ref):
    """Wraps a schema reference in the standard {data: ...} envelope."""
    return {
        "application/json": {
            "schema": {
                "type": "object",
                "properties": {"data": {"$ref": f"#/components/schemas/{ref}"}},
            }
        }
    }


def _data_list_response(ref):
    return {
        "application/json": {
            "schema": {
                "type": "object",
                "properties": {
                    "data": {"type": "array", "items": {"$ref": f"#/components/schemas/{ref}"}},
                    "meta": {
                        "type": "object",
                        "properties": {
                            "page": {"type": "integer"},
                            "per_page": {"type": "integer"},
                            "total": {"type": "integer"},
                            "has_next": {"type": "boolean"},
                        },
                    },
                },
            }
        }
    }


def _error_response():
    return {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}


COMMON_ERRORS = {
    "400": {"description": "Bad request", "content": _error_response()},
    "401": {"description": "Missing or invalid token", "content": _error_response()},
    "403": {"description": "Insufficient scope", "content": _error_response()},
    "404": {"description": "Not found", "content": _error_response()},
    "422": {"description": "Validation failed", "content": _error_response()},
    "429": {"description": "Rate limited", "content": _error_response()},
    "500": {"description": "Internal server error", "content": _error_response()},
}


def build_spec():
    """Return the full OpenAPI 3.1 spec dict."""
    host = request.host_url.rstrip("/") if request else ""
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Dikarya Public API",
            "version": "1.0.0",
            "description": (
                "Public API for Dikarya. All endpoints under `/api/v1` require a "
                "bearer token; mint one at `/user/tokens`. Tokens are scoped — see the "
                "Authentication section below."
            ),
            "contact": {"name": "Dikarya", "url": "https://dikarya.us"},
        },
        "servers": [{"url": f"{host}/api/v1"}] if host else [{"url": "/api/v1"}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "dikarya_<base64url>",
                    "description": (
                        "Provide an API token via `Authorization: Bearer dikarya_...`.\n\n"
                        "**Available scopes**:\n"
                        "- `jobs:read` — list/get jobs, read events, download files & logs\n"
                        "- `jobs:write` — create, recompute, mutate, delete jobs\n"
                        "- `tools:read` — BLAST, GenBank, MycoMap, iNaturalist lookups\n"
                        "- `account:read` — `/me` and list own tokens"
                    ),
                }
            },
            "schemas": _schemas(),
            "parameters": {
                "JobId": {
                    "name": "job_id",
                    "in": "path",
                    "required": True,
                    "description": "UUID of the job.",
                    "schema": {"type": "string", "format": "uuid"},
                },
                "Page": {
                    "name": "page",
                    "in": "query",
                    "schema": {"type": "integer", "minimum": 1, "default": 1},
                },
                "PerPage": {
                    "name": "per_page",
                    "in": "query",
                    "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                },
                "IdempotencyKey": {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": False,
                    "description": (
                        "Opaque string. If supplied, the server caches the response for "
                        "24 hours and returns the cached body on retry. Reusing the same "
                        "key with a different body yields a 409."
                    ),
                    "schema": {"type": "string", "maxLength": 200},
                },
            },
        },
        "security": [{"bearerAuth": []}],
        "tags": [
            {"name": "Account", "description": "Identity and token management"},
            {"name": "Jobs", "description": "Phylogenetic job lifecycle"},
            {"name": "Tree", "description": "Post-hoc tree mutations"},
            {"name": "Tools", "description": "Auxiliary lookups: BLAST, GenBank"},
            {"name": "Health", "description": "Liveness ping"},
        ],
        "paths": {
            "/health": {
                "get": {
                    "tags": ["Health"],
                    "summary": "Liveness ping",
                    "description": "No authentication required.",
                    "security": [],
                    "responses": {"200": {"description": "OK", "content": _data_response("User")}},
                }
            },
            "/me": {
                "get": {
                    "tags": ["Account"],
                    "summary": "Get the current user",
                    "security": [{"bearerAuth": ["account:read"]}],
                    "responses": {
                        "200": {"description": "OK", "content": _data_response("User")},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("401", "403", "429")},
                    },
                }
            },
            "/tokens": {
                "get": {
                    "tags": ["Account"],
                    "summary": "List your API tokens (no secrets)",
                    "security": [{"bearerAuth": ["account:read"]}],
                    "responses": {
                        "200": {"description": "OK", "content": _data_list_response("Token")},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("401", "403", "429")},
                    },
                }
            },
            "/jobs": {
                "get": {
                    "tags": ["Jobs"],
                    "summary": "List your jobs",
                    "security": [{"bearerAuth": ["jobs:read"]}],
                    "parameters": [
                        {"$ref": "#/components/parameters/Page"},
                        {"$ref": "#/components/parameters/PerPage"},
                        {"name": "status", "in": "query", "schema": {"type": "string"}},
                        {"name": "since", "in": "query", "schema": {"type": "string", "format": "date-time"}},
                        {"name": "until", "in": "query", "schema": {"type": "string", "format": "date-time"}},
                    ],
                    "responses": {
                        "200": {"description": "OK", "content": _data_list_response("Job")},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("400", "401", "403", "429")},
                    },
                },
                "post": {
                    "tags": ["Jobs"],
                    "summary": "Create a new phylogenetic job",
                    "security": [{"bearerAuth": ["jobs:write"]}],
                    "parameters": [{"$ref": "#/components/parameters/IdempotencyKey"}],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/CreateJobRequest"}
                            }
                        },
                    },
                    "responses": {
                        "202": {"description": "Queued", "content": _data_response("Job")},
                        "409": {"description": "Idempotency-Key reused with different body", "content": _error_response()},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("401", "403", "422", "429", "500")},
                    },
                },
            },
            "/jobs/{job_id}": {
                "parameters": [{"$ref": "#/components/parameters/JobId"}],
                "get": {
                    "tags": ["Jobs"],
                    "summary": "Get a job by id",
                    "security": [{"bearerAuth": ["jobs:read"]}],
                    "responses": {
                        "200": {"description": "OK", "content": _data_response("Job")},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("401", "403", "404", "429")},
                    },
                },
                "delete": {
                    "tags": ["Jobs"],
                    "summary": "Delete a job and its files",
                    "security": [{"bearerAuth": ["jobs:write"]}],
                    "responses": {
                        "200": {"description": "Deleted"},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("401", "403", "404", "429", "500")},
                    },
                },
            },
            "/jobs/{job_id}/recompute": {
                "post": {
                    "tags": ["Jobs"],
                    "summary": "Re-run the pipeline with new params",
                    "security": [{"bearerAuth": ["jobs:write"]}],
                    "parameters": [
                        {"$ref": "#/components/parameters/JobId"},
                        {"$ref": "#/components/parameters/IdempotencyKey"},
                    ],
                    "requestBody": {
                        "required": False,
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    },
                    "responses": {
                        "202": {"description": "Queued"},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("401", "403", "404", "429", "500")},
                    },
                }
            },
            "/jobs/{job_id}/events": {
                "get": {
                    "tags": ["Jobs"],
                    "summary": "SSE stream of pipeline progress",
                    "description": "Returns `text/event-stream`. Emits a snapshot event first, then live updates, plus 15s pings.",
                    "security": [{"bearerAuth": ["jobs:read"]}],
                    "parameters": [{"$ref": "#/components/parameters/JobId"}],
                    "responses": {
                        "200": {"description": "Event stream", "content": {"text/event-stream": {}}},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("401", "403", "404")},
                    },
                }
            },
            "/jobs/{job_id}/files": {
                "get": {
                    "tags": ["Jobs"],
                    "summary": "List downloadable artifacts",
                    "security": [{"bearerAuth": ["jobs:read"]}],
                    "parameters": [{"$ref": "#/components/parameters/JobId"}],
                    "responses": {
                        "200": {"description": "OK", "content": _data_list_response("Artifact")},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("401", "403", "404")},
                    },
                }
            },
            "/jobs/{job_id}/files/{name}": {
                "get": {
                    "tags": ["Jobs"],
                    "summary": "Download a job artifact",
                    "description": (
                        "`name` must be one of the allowlisted artifact names returned "
                        "by `/jobs/{id}/files` (e.g. `tree.newick`, `alignment.fasta`)."
                    ),
                    "security": [{"bearerAuth": ["jobs:read"]}],
                    "parameters": [
                        {"$ref": "#/components/parameters/JobId"},
                        {"name": "name", "in": "path", "required": True, "schema": {"type": "string"}},
                    ],
                    "responses": {
                        "200": {"description": "File bytes"},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("401", "403", "404")},
                    },
                }
            },
            "/jobs/{job_id}/logs/{log_name}": {
                "get": {
                    "tags": ["Jobs"],
                    "summary": "Fetch a job log",
                    "security": [{"bearerAuth": ["jobs:read"]}],
                    "parameters": [
                        {"$ref": "#/components/parameters/JobId"},
                        {
                            "name": "log_name",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "enum": ["pipeline", "alignment", "tree_builder"]},
                        },
                    ],
                    "responses": {
                        "200": {"description": "Log text"},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("401", "403", "404")},
                    },
                }
            },
            "/jobs/{job_id}/tree/prune": {
                "post": {
                    "tags": ["Tree"],
                    "summary": "Remove tips from the tree",
                    "security": [{"bearerAuth": ["jobs:write"]}],
                    "parameters": [{"$ref": "#/components/parameters/JobId"}],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {
                            "type": "object",
                            "required": ["tips"],
                            "properties": {"tips": {"type": "array", "items": {"type": "string"}}},
                        }}},
                    },
                    "responses": {
                        "200": {"description": "Updated tree state"},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("401", "403", "404", "422")},
                    },
                }
            },
            "/jobs/{job_id}/tree/rename": {
                "post": {
                    "tags": ["Tree"],
                    "summary": "Rename a tip",
                    "security": [{"bearerAuth": ["jobs:write"]}],
                    "parameters": [{"$ref": "#/components/parameters/JobId"}],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {
                            "type": "object",
                            "required": ["old_name", "new_name"],
                            "properties": {
                                "old_name": {"type": "string"},
                                "new_name": {"type": "string"},
                            },
                        }}},
                    },
                    "responses": {
                        "200": {"description": "Updated tree state"},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("401", "403", "404", "422")},
                    },
                }
            },
            "/jobs/{job_id}/tree/reroot": {
                "post": {
                    "tags": ["Tree"],
                    "summary": "Reroot using an outgroup",
                    "security": [{"bearerAuth": ["jobs:write"]}],
                    "parameters": [{"$ref": "#/components/parameters/JobId"}],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {
                            "type": "object",
                            "required": ["outgroup"],
                            "properties": {"outgroup": {"type": "string"}},
                        }}},
                    },
                    "responses": {
                        "200": {"description": "Updated tree state"},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("401", "403", "404", "422")},
                    },
                }
            },
            "/jobs/{job_id}/tree/midpoint_root": {
                "post": {
                    "tags": ["Tree"],
                    "summary": "Apply midpoint rooting",
                    "security": [{"bearerAuth": ["jobs:write"]}],
                    "parameters": [{"$ref": "#/components/parameters/JobId"}],
                    "responses": {
                        "200": {"description": "Updated tree state"},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("401", "403", "404")},
                    },
                }
            },
            "/tools/blast": {
                "post": {
                    "tags": ["Tools"],
                    "summary": "Run BLAST on a sequence or accession",
                    "security": [{"bearerAuth": ["tools:read"]}],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {
                            "type": "object",
                            "required": ["query"],
                            "properties": {
                                "query": {"type": "string", "description": "FASTA sequence or GenBank accession"},
                                "min_identity": {"type": "number", "minimum": 50, "maximum": 100, "default": 90.0},
                                "max_sequences": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                            },
                        }}},
                    },
                    "responses": {
                        "200": {"description": "BLAST results"},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("400", "401", "403", "429", "500")},
                    },
                }
            },
            "/tools/genbank": {
                "post": {
                    "tags": ["Tools"],
                    "summary": "Fetch FASTA for GenBank accessions",
                    "security": [{"bearerAuth": ["tools:read"]}],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {
                            "type": "object",
                            "properties": {
                                "accessions": {
                                    "oneOf": [
                                        {"type": "string"},
                                        {"type": "array", "items": {"type": "string"}},
                                    ],
                                    "description": "Comma/space-separated list, or array of accession strings.",
                                }
                            },
                        }}},
                    },
                    "responses": {
                        "200": {"description": "Fetched sequences"},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("401", "403", "422", "429", "500")},
                    },
                }
            },
        },
    }
