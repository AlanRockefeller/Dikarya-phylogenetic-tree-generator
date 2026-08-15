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
                        "alrt_replicates": {"type": "integer"},
                        "mcmc_generations": {"type": "integer"},
                        "mcmc_nruns": {"type": "integer"},
                        "mcmc_nchains": {"type": "integer"},
                        "mcmc_burnin_fraction": {"type": "number"},
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
        "HealthStatus": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "example": "ok"},
                "api_version": {"type": "string", "example": "v1"},
            },
        },
        "RecomputeRequest": {
            "type": "object",
            "description": (
                "Override stored job parameters and re-run the pipeline on "
                "the same input data. Only the fields listed here may be "
                "overridden; unknown keys are rejected with 422. To submit "
                "different input data, create a new job with POST /jobs."
            ),
            "additionalProperties": False,
            "properties": {
                "tree_method": {"type": "string",
                                 "enum": ["nj", "raxml", "iqtree", "mrbayes", "fasttree"]},
                "tree_model": {"type": "string", "maxLength": 64},
                "alignment_method": {"type": "string",
                                      "enum": ["mafft", "muscle", "clustalo", "iqtree_builtin", "default"]},
                "trimming_method": {"type": "string",
                                     "enum": ["none", "trimal_gappy", "trimal", "bmge"]},
                "trim_terminal_overhangs": {
                    "type": "boolean",
                    "default": True,
                    "description": "Trim alignment columns outside the common covered span before tree building.",
                },
                "bootstrap": {"type": "integer", "minimum": 0, "maximum": 10000},
                "alrt_replicates": {
                    "type": "integer", "minimum": 0, "maximum": 10000,
                    "description": "IQ-TREE SH-aLRT replicates. 0 reports UFBoot only.",
                },
                "mcmc_generations": {"type": "integer", "minimum": 1000, "maximum": 100000000},
                "mcmc_nruns": {"type": "integer", "minimum": 1, "maximum": 8},
                "mcmc_nchains": {"type": "integer", "minimum": 1, "maximum": 16},
                "mcmc_burnin_fraction": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 0.99,
                    "default": 0.25,
                    "description": "Relative fraction of MCMC samples discarded as burn-in.",
                },
                "outgroup": {"type": "string", "maxLength": 256},
                "notes": {"type": "string", "maxLength": 2000},
            },
        },
        "CreateJobRequest": {
            "type": "object",
            "example": {
                "input_type": "pasted_sequence",
                "sequence": (
                    ">Sample_A\nATGCGTACGTAGCTAGCTAGCTAGCTAGCTAACGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCG\n"
                    ">Sample_B\nATGCGTACGTAGCTAGCTAGCTAGCTAGCTAACGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGTTTGATCGATCGATCG\n"
                    ">Sample_C\nATGCGTACGTAGCTAGCTAGCTAGCTAGCTAACGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGTTTGATCG\n"
                    ">Sample_D\nATGCGTACGTAGCTAGCTAGCTAGCTAGCTAACGATCGATCGATCGATCGATCGATCGATCGATCGATCGTTTGATCGATCGATCGATCGATCG"
                ),
                "tree_method": "fasttree",
                "alignment_method": "mafft",
                "trimming_method": "trimal_gappy",
                "trim_terminal_overhangs": True,
                "notes": "API test with valid pasted FASTA",
            },
            "properties": {
                "input_type": {
                    "type": "string",
                    "default": "pasted_sequence",
                    "enum": ["pasted_sequence", "accession_list"],
                    "description": (
                        "How sequence data is provided. Use `pasted_sequence` "
                        "with FASTA text in the `sequence` field, or "
                        "`accession_list` with GenBank IDs in `accessions`. "
                        "If `sequence` is non-empty and `input_type` is omitted, "
                        "it defaults to `pasted_sequence`. Server-side FASTA "
                        "file uploads are not supported via this endpoint."
                    ),
                },
                "sequence": {
                    "type": "string",
                    "maxLength": 5000000,
                    "description": (
                        "FASTA-formatted sequence text for `pasted_sequence` jobs. "
                        "One or more `>header\\nbases` records pasted directly "
                        "into the request body. Use real DNA bases, not "
                        "placeholders. Max 5 MB; the overall request body is "
                        "capped at 16 MB (413 returned beyond that)."
                    ),
                    "example": ">Sample_A\nATGCGTACGTAGCTAGCTAGCTA\n>Sample_B\nATGCGTACGTAGCTAGCTAGCTA",
                },
                "accessions": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 64},
                    "maxItems": 500,
                    "description": "List of GenBank accession numbers. Max 500 entries, 64 chars each.",
                },
                "alignment_method": {
                    "type": "string",
                    "enum": ["mafft", "muscle", "clustalo", "iqtree_builtin", "default"],
                    "default": "mafft",
                },
                "trimming_method": {
                    "type": "string",
                    "enum": ["none", "trimal_gappy", "trimal", "bmge"],
                    "default": "trimal_gappy",
                    "description": (
                        "Alignment trimmer. 'trimal_gappy' (default) runs trimAl -gt 0.9, "
                        "dropping columns that are >90%% gaps. 'trimal' runs -automated1, "
                        "which is aggressive and strips much of ITS1/ITS2 -- not recommended "
                        "for ITS."
                    ),
                },
                "trim_terminal_overhangs": {
                    "type": "boolean",
                    "default": True,
                    "description": "Trim alignment columns outside the common covered span before tree building.",
                },
                "tree_method": {
                    "type": "string",
                    "enum": ["nj", "raxml", "iqtree", "mrbayes", "fasttree"],
                },
                "tree_model": {
                    "type": "string",
                    "description": (
                        "Substitution model. When omitted, tree_method=iqtree runs "
                        "ModelFinder (-m MFP) to select the best-fit model by BIC; "
                        "other maximum-likelihood methods use the server's "
                        "DEFAULT_ML_MODEL (normally GTR+G). The chosen IQ-TREE model "
                        "is reported as model_selected in tree_metadata.json. Pass an "
                        "explicit model name to fix it."
                    ),
                },
                "bootstrap": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 1000},
                "alrt_replicates": {
                    "type": "integer", "minimum": 0, "maximum": 10000, "default": 1000,
                    "description": "IQ-TREE SH-aLRT replicates, run alongside Ultrafast Bootstrap. Nodes are labelled SH-aLRT/UFBoot. 0 reports UFBoot only.",
                },
                "mcmc_generations": {"type": "integer", "minimum": 1000, "maximum": 100000000, "default": 50000},
                "mcmc_nruns": {"type": "integer", "minimum": 1, "maximum": 8, "default": 2},
                "mcmc_nchains": {"type": "integer", "minimum": 1, "maximum": 16, "default": 4},
                "mcmc_burnin_fraction": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 0.99,
                    "default": 0.25,
                    "description": "Relative fraction of MCMC samples discarded as burn-in.",
                },
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
    "409": {"description": "Conflict (Idempotency-Key reused with different body, or request still in flight)",
            "content": _error_response()},
    "413": {"description": "Request body exceeds the 16 MB global limit",
            "content": _error_response()},
    "422": {"description": "Validation failed", "content": _error_response()},
    "429": {"description": "Rate limited (including per-token concurrent SSE cap)",
            "content": _error_response()},
    "500": {"description": "Internal server error", "content": _error_response()},
}


def build_spec():
    """Return the full OpenAPI 3.1 spec dict."""
    host = request.host_url.rstrip("/") if request else ""
    contact_url = host or ""
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Dikarya Public API",
            "version": "1.0.0",
            "description": (
                "Public API for Dikarya. All endpoints under `/api/v1` require a "
                "bearer token; mint one at `/user/tokens`. Tokens are scoped; see the "
                "Authentication section below."
            ),
            "contact": {"name": "Dikarya", "url": contact_url},
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
                        "- `jobs:read`: list/get jobs, read events, download files & logs\n"
                        "- `jobs:write`: create, recompute, mutate, delete jobs\n"
                        "- `tools:read`: BLAST, GenBank, MycoMap, iNaturalist lookups\n"
                        "- `account:read`: `/me` and list own tokens"
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
                    "description": "No authentication required. Returns `{status, api_version}`.",
                    "security": [],
                    "responses": {"200": {"description": "OK", "content": _data_response("HealthStatus")}},
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
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("400", "401", "403", "409", "413", "422", "429", "500")},
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
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/RecomputeRequest"}}},
                    },
                    "responses": {
                        "202": {"description": "Queued"},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("400", "401", "403", "404", "409", "413", "422", "429", "500")},
                    },
                }
            },
            "/jobs/{job_id}/events": {
                "get": {
                    "tags": ["Jobs"],
                    "summary": "SSE stream of pipeline progress",
                    "description": (
                        "Returns `text/event-stream`. Emits a `snapshot` event "
                        "first, then live `data:` updates, with 15-second pings. "
                        "A single token may hold at most 5 concurrent streams "
                        "(429 `too_many_streams` beyond that). Each connection "
                        "is hard-capped at 30 minutes; on timeout the server "
                        "emits `event: timeout` and closes. Clients should "
                        "reconnect. If the token is revoked mid-stream, the "
                        "server emits `event: revoked` and closes."
                    ),
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
                            "properties": {"tips": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 10000,
                                "items": {"type": "string", "minLength": 1, "maxLength": 256},
                                "description": "Tip names (or internal-node names) to remove. Max 10 000 entries; each name max 256 chars.",
                            }},
                        }}},
                    },
                    "responses": {
                        "200": {"description": "Updated tree state"},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("401", "403", "404", "413", "422")},
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
                                "old_name": {"type": "string", "minLength": 1, "maxLength": 256},
                                "new_name": {"type": "string", "minLength": 1, "maxLength": 256,
                                              "description": "May not contain Newick-unsafe characters: ()[],:;'\"\\t\\n\\r"},
                            },
                        }}},
                    },
                    "responses": {
                        "200": {"description": "Updated tree state"},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("401", "403", "404", "413", "422")},
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
                            "properties": {"outgroup": {"type": "string", "minLength": 1, "maxLength": 256}},
                        }}},
                    },
                    "responses": {
                        "200": {"description": "Updated tree state"},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("401", "403", "404", "413", "422")},
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
                                "query": {"type": "string", "maxLength": 50000,
                                           "description": "FASTA sequence or GenBank accession. NCBI rejects very long queries; the per-call cap is 50 000 chars."},
                                "min_identity": {"type": "number", "minimum": 50, "maximum": 100, "default": 90.0,
                                                  "description": "Values outside this range are clamped, not rejected."},
                                "max_sequences": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50,
                                                   "description": "Values outside this range are clamped, not rejected."},
                            },
                        }}},
                    },
                    "responses": {
                        "200": {"description": "BLAST results"},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("400", "401", "403", "413", "422", "429", "500")},
                    },
                }
            },
            "/tools/inaturalist-tree": {
                "post": {
                    "tags": ["Tools"],
                    "summary": "Build a tree from a single iNaturalist observation",
                    "description": (
                        "Reads the observation's `Mycomap BLAST Results` "
                        "observation field, refreshes the MycoMap local BLAST "
                        "results, builds a one-click Dikarya tree from that URL, "
                        "and when the job completes writes a "
                        "`Phylogenetic Tree` field back to the observation "
                        "with the public tree viewer URL. The write uses "
                        "the site-wide authorized iNaturalist account. Dikarya "
                        "queues local MycoMap BLAST preparation in the background "
                        "with a default of 50 hits. If that automatic local "
                        "refresh fails, the job uses the saved MycoMap results. "
                        "If `rebuild_ncbi_blast` is true for a single observation, "
                        "the job queues MycoMap's asynchronous NCBI rerun and is "
                        "scheduled to resume about 10 minutes later without "
                        "occupying the phylogeny worker."
                    ),
                    "security": [{"bearerAuth": ["jobs:write"]}],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {
                            "type": "object",
                            "required": ["observation"],
                            "properties": {
                                "observation": {
                                    "type": "string",
                                    "maxLength": 300,
                                    "description": (
                                        "Either a numeric observation ID "
                                        "(e.g. `360934883`) or a single-"
                                        "observation URL "
                                        "(`https://www.inaturalist.org/observations/<id>`). "
                                        "Search URLs and multiple IDs are rejected."
                                    ),
                                    "example": "360934883",
                                },
                                "rebuild_ncbi_blast": {
                                    "type": "boolean",
                                    "default": False,
                                    "description": (
                                        "For single-observation jobs, queue a "
                                        "background MycoMap NCBI BLAST rerun, then "
                                        "schedule the tree to resume about 10 "
                                        "minutes later. Username and project batch jobs "
                                        "reject this."
                                    ),
                                },
                                "recreate_existing_tree": {
                                    "type": "boolean",
                                    "default": False,
                                    "description": (
                                        "For a single observation that already has a "
                                        "Phylogenetic Tree field, explicitly allow a new "
                                        "tree to replace the field's current URL."
                                    ),
                                },
                                "keep_existing_tree_url": {
                                    "type": "boolean",
                                    "default": False,
                                    "description": (
                                        "For a single observation that already has a "
                                        "Phylogenetic Tree field, build an additional "
                                        "tree and leave the field's current URL "
                                        "unchanged. Takes precedence over "
                                        "`recreate_existing_tree`."
                                    ),
                                },
                                "mycomap_local_limit": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 500,
                                    "default": 50,
                                    "description": (
                                        "Number of local MycoMap BLAST hits to "
                                        "request when the local BLAST results are "
                                        "rebuilt before importing sequences."
                                    ),
                                },
                                "mycomap_ncbi_limit": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 500,
                                    "default": 100,
                                    "description": (
                                        "Number of NCBI BLAST hits to request "
                                        "when rebuild_ncbi_blast is true."
                                    ),
                                }
                            },
                        }}},
                    },
                    "responses": {
                        "202": {"description": "Job queued; iNaturalist field will be updated when the tree completes."},
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("400", "401", "403", "422", "429", "500")},
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
                                        {"type": "string", "maxLength": 64000},
                                        {"type": "array", "items": {"type": "string", "maxLength": 64}, "maxItems": 200},
                                    ],
                                    "description": "Comma/space-separated list, or array of accession strings. Max 200 accessions per call.",
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
