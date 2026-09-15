"""Hand-curated OpenAPI 3.1 spec for /api/v1.

Kept as a Python dict so we can interpolate the deployed host at request
time and emit either JSON or YAML. Update this file when adding endpoints.
"""
from flask import request

from app.config import Config
from app.api_v1.job_defaults import (
    DEFAULT_ALIGNMENT_METHOD,
    DEFAULT_BOOTSTRAP,
    DEFAULT_TREE_METHOD,
)

# MrBayes defaults are quoted straight from the config so the published schema
# cannot drift from what the API actually applies.
DEFAULT_MCMC_GENERATIONS = Config.DEFAULT_MCMC_GENERATIONS
DEFAULT_MCMC_NRUNS = Config.DEFAULT_MCMC_NRNS
DEFAULT_MCMC_CHAINS = Config.DEFAULT_MCMC_CHAINS
DEFAULT_MCMC_BURNIN_FRACTION = Config.DEFAULT_MCMC_BURNIN_FRACTION
DEFAULT_MCMC_STOP_EARLY = Config.DEFAULT_MCMC_STOP_EARLY

# ...and so do the job-parameter defaults, which are re-exported from the
# module that create_job() itself applies them from. A number written here by
# hand is a number that goes stale the first time the runtime default moves:
# `alrt_replicates` was already documented as a literal 1000 while the route
# read Config.DEFAULT_IQTREE_ALRT.
DEFAULT_IQTREE_ALRT = Config.DEFAULT_IQTREE_ALRT

MCMC_STOP_EARLY_DESCRIPTION = (
    "Enable MrBayes convergence-based early stopping (mcmcdiagn=yes "
    f"stoprule=yes stopval={Config.DEFAULT_MCMC_STOPVAL}). MrBayes ends the run "
    "as soon as the average standard deviation of split frequencies between the "
    "independent runs falls below "
    f"{Config.DEFAULT_MCMC_STOPVAL}, so mcmc_generations is a maximum rather "
    "than a fixed run length. Requires mcmc_nruns >= 2: with a single run there "
    "are no independent runs to compare, no stop rule is applied, and the "
    f"analysis uses the full mcmc_generations -- which is why an omitted "
    f"mcmc_generations then defaults to "
    f"{Config.DEFAULT_MCMC_GENERATIONS_FIXED_RUN} rather than to the "
    f"{Config.DEFAULT_MCMC_GENERATIONS} ceiling. Reaching this criterion does not "
    "by itself guarantee satisfactory ESS or PSRF -- those are checked "
    "separately after the run and reported in tree_metadata.json. Defaults to "
    "true for newly submitted jobs."
)


TIMESTAMP_FILTER_DESCRIPTION = (
    "ISO-8601 timestamp filtering on {field}. An offset (`2026-08-01T12:00:00-07:00`) "
    "or a trailing `Z` is converted to UTC before the comparison; a timestamp "
    "with no offset is taken to already be UTC, which is what job timestamps "
    "are stored and returned in."
)


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
                "id": {"type": "string", "example": "aq7c"},
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
                        # serialize_job() has always returned this; the schema
                        # simply never listed it.
                        "trim_terminal_overhangs": {
                            "type": ["boolean", "null"],
                            "description": (
                                "Whether terminal-overhang trimming was applied. "
                                "Null when the option was not recorded (including "
                                "legacy jobs) or its stored value is not a "
                                "recognized boolean."
                            ),
                        },
                        "fix_orientation": {
                            "type": ["boolean", "null"],
                            "description": (
                                "Whether backwards sequences were reverse-complemented before "
                                "alignment. Null when the option was not recorded "
                                "(including legacy jobs, which ran with it on) or "
                                "its stored value is not a recognized boolean."
                            ),
                        },
                        "tree_method": {"type": "string"},
                        "tree_model": {"type": "string"},
                        "bootstrap": {"type": "integer"},
                        "alrt_replicates": {"type": "integer"},
                        "mcmc_generations": {"type": "integer"},
                        "mcmc_nruns": {"type": "integer"},
                        "mcmc_nchains": {"type": "integer"},
                        "mcmc_burnin_fraction": {"type": "number"},
                        "mcmc_stop_early": {
                            "type": "boolean",
                            "description": (
                                "Whether the job used MrBayes convergence-based "
                                "early stopping. Reported as false for jobs "
                                "created before this option existed."
                            ),
                        },
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
        "InaturalistFinderMatch": {
            "type": "object",
            "required": ["id", "url", "is_original", "location", "user", "taxon"],
            "properties": {
                "id": {"type": "integer", "example": 360934883},
                "url": {"type": "string", "format": "uri"},
                "is_original": {
                    "type": "boolean",
                    "description": "True when the supplied ID itself matched the criterion.",
                },
                "observed_on": {"type": ["string", "null"], "format": "date"},
                "location": {"type": "string"},
                "place_guess": {"type": ["string", "null"]},
                "photo_url": {"type": ["string", "null"], "format": "uri"},
                "user": {
                    "type": "object",
                    "properties": {
                        "id": {"type": ["integer", "null"]},
                        "login": {"type": ["string", "null"]},
                    },
                },
                "taxon": {
                    "type": "object",
                    "properties": {
                        "id": {"type": ["integer", "null"]},
                        "name": {"type": ["string", "null"]},
                        "rank": {"type": ["string", "null"]},
                        "preferred_common_name": {"type": ["string", "null"]},
                        "iconic_taxon_name": {"type": ["string", "null"]},
                    },
                },
            },
        },
        "InaturalistFinderResult": {
            "type": "object",
            "required": [
                "query", "criteria", "matches", "match_count",
                "checked_variations", "unchecked_variations", "total_variations",
                "original_checked", "complete", "failed_batches",
            ],
            "properties": {
                "query": {
                    "type": "object",
                    "properties": {
                        "observation_id": {"type": "string", "example": "360934883"},
                        "mode": {"type": "string", "enum": ["genus", "family", "taxon", "user", "project"]},
                        "term": {"type": "string", "example": "Beauveria"},
                        "digits_off": {"type": "integer", "minimum": 1, "maximum": 3},
                    },
                },
                "criteria": {
                    "type": "object",
                    "description": "The canonical user, project, or taxon resolved by iNaturalist.",
                },
                "matches": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/InaturalistFinderMatch"},
                },
                "match_count": {"type": "integer", "minimum": 0},
                "checked_variations": {"type": "integer", "minimum": 0},
                "unchecked_variations": {"type": "integer", "minimum": 0},
                "total_variations": {"type": "integer", "minimum": 0, "maximum": 10000},
                "original_checked": {"type": "boolean"},
                "complete": {
                    "type": "boolean",
                    "description": "False when one or more iNaturalist batches could not be checked.",
                },
                "failed_batches": {"type": "integer", "minimum": 0},
            },
        },
        "InaturalistFinderAnyResult": {
            "oneOf": [
                {"$ref": "#/components/schemas/InaturalistFinderAutoResult"},
                {"$ref": "#/components/schemas/InaturalistFinderResult"},
            ],
            "description": (
                "An automatic search returns InaturalistFinderAutoResult (`query.mode` "
                "is `auto`); a single-criterion search returns InaturalistFinderResult."
            ),
        },
        "InaturalistFinderScore": {
            "type": "object",
            "description": (
                "How many of the supplied clues this observation satisfied. `unknown` "
                "lists clues that could not be checked - today only project membership, "
                "when its request failed. An unknown clue never counts toward the score "
                "and can never make `is_full_match` true, so an unanswered question is "
                "never mistaken for a negative answer."
            ),
            "required": ["matched", "unknown", "matched_count", "total", "is_full_match"],
            "properties": {
                "matched": {
                    "type": "array", "items": {"type": "string"},
                    "example": ["genus", "user"],
                },
                "unknown": {
                    "type": "array", "items": {"type": "string"},
                    "example": ["project"],
                },
                "matched_count": {"type": "integer", "minimum": 0, "example": 2},
                "unknown_count": {"type": "integer", "minimum": 0, "example": 1},
                "total": {"type": "integer", "minimum": 0, "example": 3},
                "is_full_match": {"type": "boolean", "example": False},
            },
        },
        "InaturalistFinderAutoMatch": {
            "allOf": [
                {"$ref": "#/components/schemas/InaturalistFinderMatch"},
                {
                    "type": "object",
                    "properties": {
                        "score": {"$ref": "#/components/schemas/InaturalistFinderScore"},
                        "stage": {
                            "type": "integer", "minimum": 0,
                            "description": "Which rung found it. 0 is the number exactly as supplied.",
                        },
                    },
                },
            ],
        },
        "InaturalistFinderResume": {
            "type": "object",
            "description": (
                "Where to continue. Send `token` back as the request's `resume` to pick "
                "up exactly where this call stopped; no observation ID is requested "
                "twice. Null when the search finished, or when part of it could not be "
                "checked - a cursor over a gap would skip those IDs for good."
            ),
            "required": ["token", "stage", "offset"],
            "properties": {
                "token": {"type": "string", "example": "v1:3:0:1f4c9ab3"},
                "stage": {"type": "integer", "minimum": 1},
                "offset": {"type": "integer", "minimum": 0},
            },
        },
        "InaturalistFinderAutoResult": {
            "type": "object",
            "required": [
                "query", "status", "complete", "criteria", "unusable_clues",
                "matches", "match_count", "checked_variations",
                "unchecked_variations", "failed_batches", "stages",
            ],
            "properties": {
                "query": {
                    "type": "object",
                    "properties": {
                        "observation_id": {"type": "string", "example": "360934883"},
                        "mode": {"type": "string", "enum": ["auto"]},
                        "digits_off": {"type": "integer", "minimum": 1, "maximum": 3},
                        "clues": {
                            "type": "object", "additionalProperties": {"type": "string"},
                            "description": "The non-empty clues as received.",
                        },
                        "resumed": {"type": "boolean"},
                    },
                },
                "status": {
                    "type": "string",
                    "enum": ["match_found", "no_match", "needs_confirmation", "incomplete", "error"],
                    "description": (
                        "`match_found` at least one observation matched at least one clue; "
                        "`no_match` nothing did; `needs_confirmation` more work is available "
                        "and `resume` says where; `incomplete` part of the search could not "
                        "be checked, so a negative result is not conclusive."
                    ),
                },
                "stop_reason": {
                    "type": "string",
                    "enum": [
                        "full_match", "exhausted", "no_clues",
                        "large_stage", "budget_exhausted", "deadline_exhausted",
                        "failures",
                    ],
                },
                "message": {"type": ["string", "null"]},
                "complete": {
                    "type": "boolean",
                    "description": (
                        "True only when the ladder really finished. A paused stage and a "
                        "failed request both make this false, for different reasons."
                    ),
                },
                "criteria": {
                    "type": "array",
                    "description": "The clues that resolved and were scored, in scoring order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["genus", "family", "taxon", "user", "project"]},
                            "value": {"type": "string"},
                            "label": {"type": "string"},
                            "taxon_id": {"type": "integer"},
                        },
                    },
                },
                "notices": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Things worth telling the reader that are not errors. Today: that a "
                        "single clue matched several neighbouring observations, so the one "
                        "listed first is a best guess rather than an answer - iNaturalist "
                        "numbers observations in upload order, so adjacent numbers often "
                        "share an uploader and a taxon."
                    ),
                },
                "unusable_clues": {
                    "type": "array",
                    "description": (
                        "Clues iNaturalist could not resolve. Reported and left out of "
                        "scoring rather than failing the request, because the mistaken "
                        "element is as often a clue as the number. An ambiguous taxon name "
                        "carries its `candidates`."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string"},
                            "value": {"type": "string"},
                            "reason": {"type": "string"},
                            "candidates": {
                                "type": ["array", "null"],
                                "items": {"type": "object"},
                            },
                        },
                    },
                },
                "original": {
                    "type": ["object", "null"],
                    "description": (
                        "The observation the supplied number really points at, matching or "
                        "not, so a caller can show it even when the clues disagree. Null "
                        "when it does not exist, could not be checked, or the request "
                        "resumed past stage 0."
                    ),
                },
                "original_score": {
                    "oneOf": [
                        {"$ref": "#/components/schemas/InaturalistFinderScore"},
                        {"type": "null"},
                    ],
                },
                "original_checked": {"type": "boolean"},
                "matches": {
                    "type": "array",
                    "description": "Best first: most clues matched, ties broken by observation ID.",
                    "items": {"$ref": "#/components/schemas/InaturalistFinderAutoMatch"},
                },
                "match_count": {"type": "integer", "minimum": 0},
                "full_match_count": {
                    "type": "integer", "minimum": 0,
                    "description": "How many matched every usable clue.",
                },
                "checked_variations": {"type": "integer", "minimum": 0},
                "unchecked_variations": {
                    "type": "integer", "minimum": 0,
                    "description": "Candidates whose request failed permanently.",
                },
                "failed_batches": {"type": "integer", "minimum": 0},
                "stages": {
                    "type": "array",
                    "description": "What each rung actually did, stage 0 being the number as supplied.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "stage": {"type": "integer", "minimum": 0},
                            "total": {"type": "integer", "minimum": 0},
                            "attempted": {"type": "integer", "minimum": 0},
                            "unchecked": {"type": "integer", "minimum": 0},
                        },
                    },
                },
                "resume": {
                    "oneOf": [
                        {"$ref": "#/components/schemas/InaturalistFinderResume"},
                        {"type": "null"},
                    ],
                },
                "next_stage": {
                    "type": ["object", "null"],
                    "description": "What continuing would cost, when the search paused.",
                    "properties": {
                        "stage": {"type": "integer", "minimum": 1},
                        "label": {
                            "type": "string",
                            "example": "three substituted digits and extra digits",
                        },
                        "estimated_candidates": {"type": "integer", "minimum": 0, "example": 58968},
                        "estimated_seconds": {"type": "integer", "minimum": 0, "example": 442},
                    },
                },
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
                "fix_orientation": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Reverse-complement sequences submitted in the wrong orientation before "
                        "aligning. MAFFT does this natively; for MUSCLE and Clustal Omega, which "
                        "cannot detect direction, a short MAFFT pass runs first to decide it. "
                        "Ignored when alignment_method is 'none', since the input is already "
                        "aligned. Leave on unless the sequences are already oriented and must "
                        "not be altered."
                    ),
                },
                "trim_terminal_overhangs": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Trim alignment columns outside the common genuinely covered span before "
                        "external trimming and tree building. Terminal N/n padding counts as "
                        "missing coverage."
                    ),
                },
                "bootstrap": {
                    "type": "integer", "minimum": 0, "maximum": 10000,
                    "description": (
                        "Support replicates. For IQ-TREE UFBoot, use 0 to disable "
                        "or at least 1000; values 1-999 are invalid."
                    ),
                },
                "alrt_replicates": {
                    "type": "integer", "minimum": 0, "maximum": 10000,
                    "description": "IQ-TREE SH-aLRT replicates. 0 reports UFBoot only.",
                },
                "mcmc_generations": {
                    "type": "integer", "minimum": 1000, "maximum": 100000000,
                    "description": (
                        "MrBayes MCMC generations; the maximum whenever "
                        f"mcmc_stop_early is enabled, in which case it defaults "
                        f"to {Config.DEFAULT_MCMC_GENERATIONS}. When no stop "
                        f"rule applies (mcmc_stop_early false, or mcmc_nruns 1) "
                        f"this is the full length of the run and the default "
                        f"drops to {Config.DEFAULT_MCMC_GENERATIONS_FIXED_RUN}. "
                        "An explicitly supplied integer from 1,000 through "
                        "100,000,000 is used as given; a value outside that "
                        "range, or one that is not an integer, is rejected "
                        "with 422 rather than clamped."
                    ),
                },
                "mcmc_nruns": {"type": "integer", "minimum": 1, "maximum": 8},
                "mcmc_nchains": {"type": "integer", "minimum": 1, "maximum": 16},
                "mcmc_burnin_fraction": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 0.99,
                    "default": DEFAULT_MCMC_BURNIN_FRACTION,
                    "description": "Relative fraction of MCMC samples discarded as burn-in.",
                },
                "mcmc_stop_early": {
                    "type": "boolean",
                    "description": MCMC_STOP_EARLY_DESCRIPTION,
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
                "fix_orientation": True,
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
                    "default": DEFAULT_ALIGNMENT_METHOD,
                },
                "trimming_method": {
                    "type": "string",
                    "enum": ["none", "trimal_gappy", "trimal", "bmge"],
                    "default": Config.DEFAULT_TRIMMING_METHOD,
                    "description": (
                        "Alignment trimmer. 'trimal_gappy' (default) runs trimAl -gt 0.1, "
                        "dropping columns that are >90% gaps. 'trimal' runs -automated1, "
                        "which is aggressive and strips much of ITS1/ITS2 -- not recommended "
                        "for ITS."
                    ),
                },
                "trim_terminal_overhangs": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Trim alignment columns outside the common genuinely covered span before "
                        "external trimming and tree building. Terminal N/n padding counts as "
                        "missing coverage."
                    ),
                },
                "tree_method": {
                    "type": "string",
                    "enum": ["nj", "raxml", "iqtree", "mrbayes", "fasttree"],
                    "default": DEFAULT_TREE_METHOD,
                },
                "tree_model": {
                    "type": "string",
                    "description": (
                        "Substitution model. When omitted, tree_method=iqtree runs "
                        "ModelFinder (-m MFP) to select the best-fit model by BIC; "
                        "other maximum-likelihood methods use the server's "
                        "DEFAULT_ML_MODEL (normally GTR+G). RAxML with moose_enabled "
                        "also substitutes its own pick. Whenever the fitted model "
                        "differs from the requested one it is reported as "
                        "model_selected in tree_metadata.json, with model_selector "
                        "naming what chose it. Pass an explicit model name to fix it."
                    ),
                },
                "bootstrap": {
                    "type": "integer", "minimum": 0, "maximum": 10000,
                    "default": DEFAULT_BOOTSTRAP,
                    "description": (
                        "Support replicates. For IQ-TREE UFBoot, use 0 to disable "
                        "or at least 1000; values 1-999 are invalid."
                    ),
                },
                "alrt_replicates": {
                    "type": "integer", "minimum": 0, "maximum": 10000,
                    "default": DEFAULT_IQTREE_ALRT,
                    "description": "IQ-TREE SH-aLRT replicates, run alongside Ultrafast Bootstrap. Nodes are labelled SH-aLRT/UFBoot. 0 reports UFBoot only.",
                },
                "mcmc_generations": {
                    "type": "integer", "minimum": 1000, "maximum": 100000000,
                    # Deliberately no "default": the effective one depends on
                    # mcmc_stop_early and mcmc_nruns, and advertising a single
                    # unconditional value told callers the server would use a
                    # number it often does not.
                    "description": (
                        "MrBayes MCMC generations. Whenever the convergence "
                        "stop rule applies -- mcmc_stop_early enabled (the "
                        "default) together with mcmc_nruns >= 2 -- this is a "
                        "maximum, the run may finish substantially earlier, and "
                        f"an omitted value defaults to {DEFAULT_MCMC_GENERATIONS}. "
                        "When no stop rule applies (mcmc_stop_early false, or "
                        "mcmc_nruns 1) this is the full length of the run, and "
                        "an omitted value instead defaults to "
                        f"{Config.DEFAULT_MCMC_GENERATIONS_FIXED_RUN}. An "
                        "explicitly supplied integer from 1,000 through "
                        "100,000,000 is used as given; a value outside that "
                        "range, or one that is not an integer, is rejected "
                        "with 422 rather than clamped. Server-side job "
                        "runtime limits still apply."
                    ),
                },
                "mcmc_nruns": {
                    "type": "integer", "minimum": 1, "maximum": 8,
                    "default": DEFAULT_MCMC_NRUNS,
                },
                "mcmc_nchains": {
                    "type": "integer", "minimum": 1, "maximum": 16,
                    "default": DEFAULT_MCMC_CHAINS,
                },
                "mcmc_burnin_fraction": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 0.99,
                    "default": DEFAULT_MCMC_BURNIN_FRACTION,
                    "description": "Relative fraction of MCMC samples discarded as burn-in.",
                },
                "mcmc_stop_early": {
                    "type": "boolean",
                    "default": DEFAULT_MCMC_STOP_EARLY,
                    "description": MCMC_STOP_EARLY_DESCRIPTION,
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
                    "description": (
                        "Job id. Jobs created before 2026-09-09 are UUID4; "
                        "newer ones are a short base36 string such as 'aq7c'. "
                        "Treat it as an opaque token."
                    ),
                    "schema": {"type": "string", "example": "aq7c"},
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
            {"name": "Tools", "description": "Auxiliary lookups: BLAST, GenBank, and iNaturalist"},
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
                        {"name": "since", "in": "query",
                         "schema": {"type": "string", "format": "date-time"},
                         "description": TIMESTAMP_FILTER_DESCRIPTION.format(field="created_at >=")},
                        {"name": "until", "in": "query",
                         "schema": {"type": "string", "format": "date-time"},
                         "description": TIMESTAMP_FILTER_DESCRIPTION.format(field="created_at <")},
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
                        "(429 `too_many_streams` beyond that). The server closes "
                        "a stream with `event: timeout` when it reaches the "
                        "absolute lifetime cap (`reason: max_duration_reached`) "
                        "or when it has seen no activity on a still-running job "
                        "for the idle limit (`reason: idle`); both carry "
                        "`max_seconds`. Clients should reconnect -- an "
                        "`EventSource` does so automatically and receives a "
                        "fresh snapshot. Connecting to a job that has already "
                        "finished or failed yields its snapshot, a short linger, "
                        "then close. If the token is revoked mid-stream, the "
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
                                              "description": "May not contain control characters or Newick-unsafe punctuation: ()[],:;'\""},
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
            "/tools/inaturalist-finder": {
                "post": {
                    "tags": ["Tools"],
                    "summary": "Find an iNaturalist observation with mistyped digits",
                    "description": (
                        "Two search modes share this endpoint.\n\n"
                        "**Automatic search (recommended).** Omit `mode` and send any "
                        "combination of the clue fields `genus`, `family`, `taxon`, `user` "
                        "and `project` - or none at all. The search checks the number "
                        "exactly as supplied first, then widens through one, two and three "
                        "substituted digits, adjacent swaps and missing or extra digits, "
                        "stopping the moment an observation matches every usable clue. "
                        "Observations matching only some clues are returned too, ranked by "
                        "how many matched and annotated with `score`, because a clue can be "
                        "wrong as easily as a digit. A clue iNaturalist cannot resolve is "
                        "reported in `unusable_clues` and dropped rather than failing the "
                        "request; malformed input is still a 422, and an unreachable "
                        "iNaturalist is still an upstream error.\n\n"
                        "**Bounded work, with resume.** A single request checks at most "
                        "10,000 observation numbers, spends at most 150 seconds, and never "
                        "starts a stage wider than 5,000 without being asked. The time "
                        "limit is separate from the candidate limit because pacing, "
                        "rate-limit retries and project-membership lookups make a candidate "
                        "count a poor predictor of duration. When any limit is reached the "
                        "response is `status: \"needs_confirmation\"` with a `resume` cursor "
                        "and a `next_stage` estimate; repeat the request with that cursor "
                        "(and `confirm: true` for a large stage) to continue from exactly "
                        "where it stopped. Nothing is ever re-requested, because the cursor "
                        "replays candidate generation offline. The widest stage of a "
                        "nine-digit number is ~59,000 numbers, which is why it is never run "
                        "unasked.\n\n"
                        "**Single-criterion search (unchanged).** Send `mode` and `term` for "
                        "the original behaviour: exactly one criterion, every result required "
                        "to match it, every candidate checked. Sending both `mode` and clue "
                        "fields is a 422.\n\n"
                        "`complete: false` means the search did not establish that the "
                        "unchecked candidates have no matches - because a request failed, or "
                        "because it is waiting to be resumed. Call this endpoint from the "
                        "integrating website's server; never expose its bearer token in "
                        "browser JavaScript."
                    ),
                    "security": [{"bearerAuth": ["tools:read"]}],
                    "parameters": [{"$ref": "#/components/parameters/IdempotencyKey"}],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["observation"],
                                "properties": {
                                    "observation": {
                                        "oneOf": [
                                            {"type": "string", "maxLength": 300},
                                            {"type": "integer", "minimum": 1},
                                        ],
                                        "description": "Potentially mistyped observation ID or full iNaturalist observation URL.",
                                        "example": "360934883",
                                    },
                                    "genus": {
                                        "type": "string", "maxLength": 200,
                                        "description": "Automatic search: expected genus name. Optional.",
                                        "example": "Amanita",
                                    },
                                    "family": {
                                        "type": "string", "maxLength": 200,
                                        "description": "Automatic search: expected family name. Optional.",
                                    },
                                    "taxon": {
                                        "type": "string", "maxLength": 200,
                                        "description": (
                                            "Automatic search: expected taxon ID or taxon URL, matching "
                                            "that taxon and everything below it. Optional. A value that "
                                            "is not a taxon ID is a 422 even in automatic search."
                                        ),
                                        "example": "48419",
                                    },
                                    "user": {
                                        "type": "string", "maxLength": 200,
                                        "description": "Automatic search: expected observer's iNaturalist username. Optional.",
                                    },
                                    "project": {
                                        "type": "string", "maxLength": 200,
                                        "description": (
                                            "Automatic search: project ID, slug, URL, or exact title. "
                                            "Optional. When combined with another clue, membership is "
                                            "checked with a separate request so the other clues are not "
                                            "hidden, and is reported as match, no match, or unknown."
                                        ),
                                    },
                                    "resume": {
                                        "type": "string",
                                        "description": (
                                            "Automatic search: the `resume.token` from a previous "
                                            "`needs_confirmation` response. Continues from exactly where "
                                            "that request stopped without re-requesting any ID. Bound to "
                                            "the observation number, the clues and `digits_off`."
                                        ),
                                        "example": "v1:2:0:1f4c9ab3",
                                    },
                                    "confirm": {
                                        "type": "boolean",
                                        "default": False,
                                        "description": (
                                            "Automatic search: run a stage wider than 5,000 candidates. "
                                            "Without it such a stage is never started, and the response "
                                            "reports `needs_confirmation` with an estimate instead."
                                        ),
                                    },
                                    "mode": {
                                        "type": "string",
                                        "enum": ["genus", "family", "taxon", "user", "project"],
                                        "description": (
                                            "Single-criterion search. Requires `term`, and may not be "
                                            "combined with the clue fields above."
                                        ),
                                        "example": "genus",
                                    },
                                    "term": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 200,
                                        "description": (
                                            "Single-criterion search: exact genus/family name, taxon ID "
                                            "or URL, exact username, or project ID/slug/URL/exact title. "
                                            "Ambiguous taxa are returned in the 422 error's "
                                            "`details.candidates` list."
                                        ),
                                        "example": "Amanita",
                                    },
                                    "digits_off": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": 3,
                                        "description": (
                                            "How wide the search may go. Defaults to 3 for an automatic "
                                            "search and 1 for a single-criterion search."
                                        ),
                                    },
                                },
                            },
                            "examples": {
                                "automatic": {
                                    "summary": "Automatic search with several clues",
                                    "value": {
                                        "observation": "360934883",
                                        "genus": "Beauveria",
                                        "user": "alan_rockefeller",
                                    },
                                },
                                "automaticNoClues": {
                                    "summary": "Automatic search with no clues (checks only the number as supplied)",
                                    "value": {"observation": "360934883"},
                                },
                                "automaticResume": {
                                    "summary": "Continuing a paused deeper search",
                                    "value": {
                                        "observation": "360934883",
                                        "genus": "Beauveria",
                                        "resume": "v1:3:0:1f4c9ab3",
                                        "confirm": True,
                                    },
                                },
                                "singleCriterion": {
                                    "summary": "Original single-criterion search",
                                    "value": {
                                        "observation": "360934883",
                                        "mode": "genus",
                                        "term": "Beauveria",
                                        "digits_off": 1,
                                    },
                                },
                            },
                        }},
                    },
                    "responses": {
                        "200": {
                            "description": (
                                "Search finished, paused, or finished partially; inspect "
                                "`data.status` and `data.complete`. An automatic search returns "
                                "`InaturalistFinderAutoResult`, a single-criterion search returns "
                                "`InaturalistFinderResult`."
                            ),
                            "content": _data_response("InaturalistFinderAnyResult"),
                        },
                        **{k: v for k, v in COMMON_ERRORS.items() if k in ("400", "401", "403", "409", "413", "422", "429", "500")},
                        "502": {"description": "iNaturalist was unavailable", "content": _error_response()},
                        "503": {"description": "The shared iNaturalist request queue was busy", "content": _error_response()},
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
