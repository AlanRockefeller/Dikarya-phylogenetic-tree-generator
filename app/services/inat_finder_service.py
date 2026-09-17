"""Server-side iNaturalist observation-number finder for the public API.

The browser finder talks directly to iNaturalist.  This module intentionally
keeps the public API implementation server-side so another website only needs a
Dikarya token, while preserving the browser finder's candidate generation and
matching rules.
"""
from __future__ import annotations

import hashlib
import itertools
import re
import time
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from app.services.inaturalist_tree_service import (
    InatDeadlineExceeded, InatTreeError, _http_request, remaining_seconds,
)


INAT_API_BASE = "https://api.inaturalist.org/v1"
BATCH_SIZE = 200
MAX_API_VARIATIONS = 10_000
MAX_CONSECUTIVE_FAILED_BATCHES = 4
VALID_MODES = frozenset({"genus", "family", "taxon", "user", "project"})

# --- Bounded auto mode -----------------------------------------------------
# The clues an auto search may combine, in the order they are verified, scored
# and listed. Matches inat.finder.py 1.8.0's order so a "2 of 3: genus, user"
# reads the same here as it does from the CLI.
AUTO_CLUE_KINDS = ("genus", "family", "taxon", "user", "project")
# 1.8.0 caps the ladder at three substituted digits by default.
AUTO_DEFAULT_MAX_DIGITS = 3
# A stage wider than this asks the caller before it runs, exactly as the CLI does.
LARGE_SEARCH_THRESHOLD = 5_000
# 1.8.1: above this many candidates a stage stops at the batch that produced a
# full match; below it the stage always runs to the end.
#
# Finishing is the default because "the first full match" is weak evidence:
# iNaturalist assigns observation numbers in upload order, so the numbers either
# side of a mistyped one very often share an uploader and frequently a taxon.
# With a single clue that coincidence satisfies a full match, and the stage used
# to abort on it while the observation actually wanted sat further down the same
# stage, never requested.
EARLY_STOP_MIN_CANDIDATES = 5_000
# How much work one HTTP request may do. This is the "bounded" in bounded auto:
# the widest stage of a nine-digit number is ~59,000 candidates, which is about
# seven minutes of batches and cannot be held in a request slot. When the budget
# runs out the search returns a resume cursor instead of a timeout, and the
# caller continues with another call.
MAX_REQUEST_CANDIDATES = 10_000
# How much TIME one HTTP request may spend. The candidate budget above bounds
# work, not duration: a batch can sit through the shared iNaturalist pacer, a
# 429 retry schedule, a 20-second socket timeout and a second membership
# request, so 10,000 candidates is not a promise about the clock. Without this
# a deep search could outlive nginx's proxy_read_timeout (300s) and the caller
# would lose the resume cursor along with the connection - the one thing that
# makes the search continuable. 150s leaves comfortable headroom under that.
MAX_REQUEST_SECONDS = 150.0
# Time reserved before STARTING another batch, so the search stops at a batch
# boundary -- where the plan position is exact and a cursor resumes it without
# skipping anything -- rather than partway through one.
#
# This is a PROGRESS heuristic, not the safety guarantee. It cannot be the
# guarantee: a batch's real cost is pacing (up to MAX_PACING_WAIT_SECONDS) plus
# a socket timeout, possibly twice, plus a Retry-After backoff, and reserving
# the true worst case would be most of the budget. The guarantee lives one
# layer down, in `_http_request`, which re-checks the deadline before pacing,
# after pacing, before every retry sleep and before every socket read, and
# clamps each attempt's timeout to what is actually left. So overshooting this
# reserve costs a stopped batch, never an overrun request.
BATCH_TIME_RESERVE_SECONDS = 45.0
# Added when a separate project-membership request follows each batch, since
# that is a second paced round trip before the loop comes back here.
MEMBERSHIP_TIME_RESERVE_SECONDS = 25.0
# The socket timeout an iNaturalist call gets when time is plentiful; the
# deadline lowers it per attempt. The floor is the point below which starting
# another call is pointless.
INAT_REQUEST_TIMEOUT_SECONDS = 20.0
MIN_INAT_REQUEST_TIMEOUT_SECONDS = 5.0
# Bumped when candidate generation changes in a way that moves plan positions,
# so an old cursor can never be replayed against a new ladder.
CANDIDATE_GENERATION_VERSION = 1
RESUME_TOKEN_VERSION = 1


class FinderValidationError(ValueError):
    """A caller-correctable finder request error.

    ``malformed`` separates "this value cannot be read at all" from "iNaturalist
    has no such genus". Auto mode drops the second kind of clue and carries on
    with the rest; the first kind is fatal in both modes, and an
    :class:`InatTreeError` is an outage that is never either.
    """

    def __init__(self, message: str, *, details: Optional[dict] = None, malformed: bool = False):
        super().__init__(message)
        self.details = details
        self.malformed = malformed


def parse_observation_id(value: Any) -> str:
    """Return a canonical ID from a number or observation URL."""
    text = str(value or "").strip()
    if text.isdigit():
        candidate = text
    else:
        match = re.fullmatch(
            r"(?:https?://)?(?:www\.)?inaturalist\.org/observations/(\d+)"
            r"/?(?:[?#].*)?",
            text,
            flags=re.IGNORECASE,
        )
        candidate = match.group(1) if match else ""
    candidate = candidate.lstrip("0") or ("0" if candidate else "")
    if not candidate or candidate == "0" or len(candidate) > 12:
        raise FinderValidationError(
            "`observation` must be a positive numeric iNaturalist observation "
            "ID or a full iNaturalist observation URL.",
            details={"field": "observation"},
        )
    return candidate


def _is_valid_candidate(value: str) -> bool:
    """True when a digit string is a usable observation ID (no leading zero)."""
    return bool(value) and (len(value) == 1 or not value.startswith("0"))


def _replacement_digits(number: str, index: int) -> List[str]:
    """Digits that may replace ``number[index]`` without creating a leading zero."""
    first = 1 if index == 0 and len(number) > 1 else 0
    return [str(digit) for digit in range(first, 10) if str(digit) != number[index]]


def _iter_digit_variations(number: str, digits_off: int) -> Iterable[str]:
    """Yield candidates differing from ``number`` in one to ``digits_off`` places.

    A generator on purpose: the replacement space grows combinatorially and must
    never be materialized in full for a deep search.
    """
    if digits_off <= 0:
        yield number
        return
    length = len(number)
    for change_count in range(1, min(digits_off, length) + 1):
        for positions in itertools.combinations(range(length), change_count):
            choices = [_replacement_digits(number, position) for position in positions]
            for replacements in itertools.product(*choices):
                candidate = list(number)
                for position, replacement in zip(positions, replacements):
                    candidate[position] = replacement
                joined = "".join(candidate)
                # Only reachable when the input itself has a leading zero.
                if _is_valid_candidate(joined):
                    yield joined


def count_digit_variations(number: str, digits_off: int) -> int:
    """Count replacement variations exactly, without generating them.

    A stage has to announce its real size before building a single candidate,
    both to decide whether to ask the caller first and to report an honest
    ``estimated_candidates``. Computed with the same small polynomial DP the CLI
    uses so the two never disagree about how big a stage is.
    """
    length = len(number)
    if digits_off <= 0 or length == 0:
        return 0
    # Coefficient k of ``counts`` is the number of variations changing k digits.
    counts = [1]
    for index in range(length):
        options = len(_replacement_digits(number, index))
        # A leading-zero input can only produce valid IDs by changing position 0.
        forced = index == 0 and length > 1 and number[0] == "0"
        updated = [0] * (len(counts) + 1)
        for changed, total in enumerate(counts):
            if not forced:
                updated[changed] += total
            updated[changed + 1] += total * options
        counts = updated
    return sum(counts[1:min(digits_off, length) + 1])


def _iter_digit_insertions(number: str, max_added: int = 2) -> Iterable[str]:
    """One, then two, missing digits restored at every position."""
    seen: Set[str] = set()

    def offer(value: str) -> bool:
        if not _is_valid_candidate(value) or value in seen:
            return False
        seen.add(value)
        return True

    one_digit = [
        number[:position] + str(digit) + number[position:]
        for position in range(len(number) + 1)
        for digit in range(10)
    ]
    for candidate in one_digit:
        if offer(candidate):
            yield candidate
    if max_added >= 2:
        # Bases with a leading zero are still expanded, because a second leading
        # digit can make them valid again ("0123" -> "50123").
        for base in one_digit:
            for position in range(len(base) + 1):
                for digit in range(10):
                    candidate = base[:position] + str(digit) + base[position:]
                    if offer(candidate):
                        yield candidate


def _generate_digit_removals(number: str, max_removed: int = 2) -> List[str]:
    """One or two extra digits removed from any position."""
    length = len(number)
    if not length:
        return []
    variations = set()
    for remove_count in range(1, min(max_removed, length) + 1):
        for keep in itertools.combinations(range(length), length - remove_count):
            candidate = "".join(number[index] for index in keep)
            if _is_valid_candidate(candidate):
                variations.add(candidate)
    return _unique_by_integer_value(sorted(variations))


def _generate_digit_transpositions(number: str) -> List[str]:
    """Two adjacent digits typed the wrong way round (123456789 -> 123465789)."""
    seen: Set[str] = set()
    variations = []
    for index in range(len(number) - 1):
        if number[index] == number[index + 1]:
            continue
        candidate = number[:index] + number[index + 1] + number[index] + number[index + 2:]
        if not _is_valid_candidate(candidate) or candidate in seen:
            continue
        seen.add(candidate)
        variations.append(candidate)
    return variations


def _unique_by_integer_value(sequence: Iterable[str]) -> List[str]:
    """Deduplicate digit strings by numeric ID, preserving first-seen order."""
    seen: Set[str] = set()
    result = []
    for item in sequence:
        if not isinstance(item, str) or not item.isdigit():
            continue
        if len(item) > 1 and item.startswith("0"):
            continue
        # Leading zeroes are already excluded, so the string is the value's one
        # canonical spelling and compares exactly as int() would.
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


class CandidatePlan:
    """A deduplicated search space, sized up front and streamed on demand.

    The candidate classes cannot collide: insertions and removals change the
    number's length, and transpositions - which do not - are only added below two
    substituted digits, because a two-digit replacement search already contains
    every adjacent swap. So no observation ID is ever requested twice and
    ``total`` is the true number of API-checked candidates.

    The iteration ORDER is part of the contract, not an implementation detail: a
    resume cursor is a position in this sequence, so a plan that yielded the same
    candidates in a different order would make an old cursor skip or repeat work.
    ``tests/test_inat_finder_api.py`` pins the order against the vendored CLI.
    """

    def __init__(self, number: str, digits_off: int, add_digits: bool, remove_digits: bool):
        self.number = number
        self.digits_off = digits_off
        self.replacement_count = count_digit_variations(number, digits_off)
        self.additions: List[str] = []
        self.removals: List[str] = []
        self.transpositions: List[str] = []
        if digits_off > 0:
            if add_digits:
                self.additions = list(_iter_digit_insertions(number, 2))
            if remove_digits:
                self.removals = _generate_digit_removals(number, 2)
            if digits_off < 2:
                self.transpositions = _generate_digit_transpositions(number)

        seen = {number} if number.isdigit() and number else set()
        self.extras: List[str] = []
        for candidate in itertools.chain(self.additions, self.removals, self.transpositions):
            if candidate in seen:
                continue
            seen.add(candidate)
            self.extras.append(candidate)
        self.total = self.replacement_count + len(self.extras)

    def __len__(self) -> int:
        return self.total

    def __iter__(self) -> Iterable[str]:
        if self.digits_off > 0:
            yield from _iter_digit_variations(self.number, self.digits_off)
        yield from self.extras


def build_candidate_plan(number: str, digits_off: int) -> CandidatePlan:
    """Build one rung's plan. Insertions only make sense below nine digits and
    removals only above five, so those switches come from the number itself."""
    return CandidatePlan(
        number,
        digits_off,
        add_digits=digits_off > 0 and len(number) < 9,
        remove_digits=digits_off > 0 and len(number) > 5,
    )


_STAGE_ORDINALS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def auto_stage_label(index: int, plan: CandidatePlan) -> str:
    """Describe a rung by what it really searches.

    Derived from the plan rather than hard-coded, because a plan does not add one
    edit class per ``digits_off``: it always tries up to two inserted and two
    removed digits when those classes are enabled at all, and contributes
    adjacent swaps only below two substituted digits.
    """
    if index <= 0:
        return "the number exactly as supplied"
    ordinal = _STAGE_ORDINALS.get(index, str(index))
    parts = [f"{ordinal} substituted {'digit' if index == 1 else 'digits'}"]
    if plan.transpositions:
        parts.append("adjacent swaps")
    if plan.additions and plan.removals:
        parts.append("missing or extra digits")
    elif plan.additions:
        parts.append("missing digits")
    elif plan.removals:
        parts.append("extra digits")
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


class AutoStage:
    """One rung of the ladder: a plan minus every candidate already tried.

    The plans nest as sets - plan(n,1) < plan(n,2) < plan(n,3) - so stage k is
    plan k with everything an earlier stage yielded filtered out. ``total`` is
    ``plan.total - len(seen_ids)`` measured when the stage starts, which is the
    count that matches what will really be requested; a stage can end early and
    leave candidates that never entered ``seen_ids`` for the next stage.

    ``plan_position`` counts entries pulled from the *plan*, not candidates
    yielded, because that is the position a resume cursor has to replay.
    """

    def __init__(self, index: int, plan: CandidatePlan, seen_ids: Set[str]):
        self.index = index
        self.plan = plan
        self.seen_ids = seen_ids
        self.label = auto_stage_label(index, plan)
        self.total = max(0, plan.total - len(seen_ids))
        self.plan_position = 0

    def __len__(self) -> int:
        return self.total

    def __iter__(self) -> Iterable[str]:
        for position, candidate in enumerate(self.plan, start=1):
            self.plan_position = position
            if candidate in self.seen_ids:
                continue
            self.seen_ids.add(candidate)
            yield candidate

    def exhausted(self) -> bool:
        return self.plan_position >= self.plan.total


def estimate_stage_seconds(total: int, membership_project_id: Optional[str] = None) -> int:
    """Roughly how long a stage takes. A shared project costs a second request
    per batch, since the main request must not be filtered by it."""
    batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    if membership_project_id:
        batches *= 2
    return int(batches * 1.5)


def search_fingerprint(number: str, criteria: List[dict], digits_cap: int) -> str:
    """A short hash binding a resume cursor to the search that produced it.

    A bare "stage 2, offset 400" cursor is meaningless - worse, silently wrong -
    replayed against a different number, a different set of clues, or a build
    whose candidate order has changed. This is not a secret and does not need to
    be; it exists so a mismatch is an error rather than a search that quietly
    skips the wrong candidates.
    """
    parts = [str(CANDIDATE_GENERATION_VERSION), number, str(digits_cap)]
    parts.extend(sorted(f"{item['kind']}={item['value']}" for item in criteria))
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:8]


def build_resume_token(stage: int, offset: int, fingerprint: str) -> str:
    """Format a resume cursor as ``v1:<stage>:<offset>:<fingerprint>``."""
    return f"v{RESUME_TOKEN_VERSION}:{stage}:{offset}:{fingerprint}"


def parse_resume_token(token: Any) -> Tuple[int, int, str]:
    """Return ``(stage, offset, fingerprint)`` from a resume cursor."""
    invalid = FinderValidationError(
        f"`resume` is not a resume cursor this release understands: {token!r}.",
        details={"field": "resume"},
        malformed=True,
    )
    if not isinstance(token, str):
        raise invalid
    parts = token.strip().split(":")
    if len(parts) != 4:
        raise invalid
    version, stage, offset, fingerprint = parts
    if not version.startswith("v") or not version[1:].isdigit():
        raise invalid
    if int(version[1:]) != RESUME_TOKEN_VERSION:
        raise FinderValidationError(
            f"`resume` cursor version {version[1:]} is not supported by this release.",
            details={"field": "resume", "supported_version": RESUME_TOKEN_VERSION},
            malformed=True,
        )
    if not stage.isdigit() or not offset.isdigit() or not fingerprint:
        raise invalid
    return int(stage), int(offset), fingerprint


def restore_seen_ids(number: str, stage_index: int, offset: int) -> Set[str]:
    """Rebuild the already-tried set a cursor implies. Makes no API calls.

    Replaying is exact rather than approximate: candidate generation is
    deterministic, so walking the earlier plans in full and the first ``offset``
    entries of the cursor's own plan reproduces precisely the IDs the original
    call had already put in ``seen_ids``.
    """
    seen: Set[str] = set()
    for index in range(1, stage_index):
        for candidate in build_candidate_plan(number, index):
            seen.add(candidate)
    if offset > 0 and stage_index > 0:
        for position, candidate in enumerate(build_candidate_plan(number, stage_index)):
            if position >= offset:
                break
            seen.add(candidate)
    return seen


def build_variations(number: str, digits_off: int) -> List[str]:
    """The single-criterion search's candidate list.

    Delegates to the same plan the ladder uses, so the two paths can never
    disagree about what a "one wrong digit" search covers. The plan's order is
    immaterial here - this path checks every candidate before answering - but its
    membership is exactly the CLI's.
    """
    plan = build_candidate_plan(number, digits_off)
    if plan.total > MAX_API_VARIATIONS:
        raise FinderValidationError(
            f"This search produces more than {MAX_API_VARIATIONS:,} candidate "
            "observation IDs. Use fewer potentially wrong digits.",
            details={"field": "digits_off", "max_variations": MAX_API_VARIATIONS},
        )
    return list(plan)


def _api_get(
    path: str,
    params: Optional[dict] = None,
    *,
    allow_missing: bool = False,
    max_attempts: int = 2,
    deadline: Optional[float] = None,
) -> Optional[dict]:
    query = urllib.parse.urlencode(params or {})
    url = f"{INAT_API_BASE}{path}" + (f"?{query}" if query else "")
    try:
        return _http_request(
            url, max_attempts=max_attempts,
            timeout=INAT_REQUEST_TIMEOUT_SECONDS, deadline=deadline,
        )
    except InatTreeError as exc:
        if allow_missing and "HTTP 404" in str(exc):
            return None
        raise


def _taxon_summary(taxon: dict) -> dict:
    return {
        key: taxon.get(key)
        for key in ("id", "name", "rank", "preferred_common_name", "iconic_taxon_name")
        if taxon.get(key) is not None
    }


def resolve_criteria(mode: str, term: str, deadline: Optional[float] = None) -> dict:
    """Resolve and validate a finder criterion against iNaturalist.

    Takes the deadline because this runs BEFORE the candidate ladder and is
    itself several paced, retrying API calls: up to five clues, two lookups
    each for a genus or family. A deadline that only started being honoured at
    the first batch would already have been spent by the time it was checked.
    """
    if mode == "taxon":
        match = re.search(r"(?:^|/taxa/)(\d+)", term)
        taxon_id = int(match.group(1)) if match else 0
        if taxon_id <= 0:
            # Not a clue that turned out to be wrong - a value that is not a
            # taxon ID at all. Fatal in auto mode too, where every other kind of
            # unresolvable clue is merely dropped.
            raise FinderValidationError(
                "`term` must be a positive iNaturalist taxon ID or taxon URL in taxon mode.",
                details={"field": "term"},
                malformed=True,
            )
        data = _api_get(f"/taxa/{taxon_id}", allow_missing=True, deadline=deadline)
        taxon = next(
            (item for item in (data or {}).get("results", [])
             if isinstance(item, dict) and str(item.get("id")) == str(taxon_id)),
            None,
        )
        if not taxon:
            raise FinderValidationError(
                f"iNaturalist taxon ID {taxon_id} was not found.",
                details={"field": "term"},
            )
        rank = f" ({taxon.get('rank')})" if taxon.get("rank") else ""
        return {"label": f"{taxon.get('name') or f'Taxon {taxon_id}'}{rank}",
                "taxon_id": taxon_id, "taxon": _taxon_summary(taxon)}

    if mode in {"genus", "family"}:
        exact: Dict[str, dict] = {}
        for path in ("/taxa/autocomplete", "/taxa"):
            try:
                data = _api_get(path, {"q": term, "rank": mode, "per_page": 30},
                                deadline=deadline)
            except InatTreeError:
                if not exact:
                    raise
                break
            for item in (data or {}).get("results", []):
                if not isinstance(item, dict) or item.get("rank") != mode:
                    continue
                if str(item.get("name") or "").casefold() != term.casefold():
                    continue
                try:
                    taxon_id = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                exact.setdefault(str(taxon_id), item)
            if len(exact) > 1:
                break
        if len(exact) > 1:
            candidates = [_taxon_summary(item) for item in exact.values()]
            raise FinderValidationError(
                f"{mode.title()} `{term}` matches more than one iNaturalist taxon. "
                "Retry in taxon mode with one of the returned IDs.",
                details={"field": "term", "candidates": candidates},
            )
        if not exact:
            raise FinderValidationError(
                f"{mode.title()} `{term}` was not found in the iNaturalist taxonomy.",
                details={"field": "term"},
            )
        taxon = next(iter(exact.values()))
        taxon_id = int(taxon["id"])
        return {"label": f"{taxon['name']} (taxon ID {taxon_id})",
                "taxon_id": taxon_id, "taxon": _taxon_summary(taxon)}

    if mode == "user":
        data = _api_get(f"/users/{urllib.parse.quote(term, safe='')}",
                        allow_missing=True, deadline=deadline)
        user = next(
            (item for item in (data or {}).get("results", [])
             if isinstance(item, dict)
             and str(item.get("login") or "").casefold() == term.casefold()),
            None,
        )
        if not user:
            raise FinderValidationError(
                f"iNaturalist user `{term}` was not found.", details={"field": "term"}
            )
        return {"label": user["login"], "user": {"id": user.get("id"), "login": user["login"]}}

    project_match = re.search(r"(?:^|/)projects/([^/?#]+)", term, flags=re.IGNORECASE)
    direct = term if term.isdigit() else (project_match.group(1) if project_match else term if " " not in term else "")
    if direct:
        data = _api_get(f"/projects/{urllib.parse.quote(direct, safe='')}",
                            allow_missing=True, deadline=deadline)
        project = next((item for item in (data or {}).get("results", []) if isinstance(item, dict)), None)
        if project:
            return {"label": project.get("title") or project.get("slug") or direct,
                    "project_id": str(project.get("id") or direct),
                    "project": {key: project.get(key) for key in ("id", "slug", "title")}}
        if term.isdigit() or project_match:
            raise FinderValidationError(
                f"iNaturalist project `{term}` was not found.", details={"field": "term"}
            )

    data = _api_get("/projects",
                    {"q": project_match.group(1) if project_match else term, "per_page": 10},
                    deadline=deadline)
    query = (project_match.group(1) if project_match else term).casefold()
    exact = [
        item for item in (data or {}).get("results", [])
        if isinstance(item, dict) and query in {
            str(item.get("slug") or "").casefold(), str(item.get("title") or "").casefold()
        }
    ]
    if len(exact) != 1:
        details = {"field": "term"}
        if exact:
            details["candidates"] = [
                {key: item.get(key) for key in ("id", "slug", "title")} for item in exact
            ]
        raise FinderValidationError(
            f"Project `{term}` did not resolve to one exact iNaturalist project. "
            "Use its numeric ID or exact slug.",
            details=details,
        )
    project = exact[0]
    return {"label": project.get("title") or project.get("slug"),
            "project_id": str(project["id"]),
            "project": {key: project.get(key) for key in ("id", "slug", "title")}}


def _taxon_ancestry_matches(observation: dict, target_taxon_id: Any) -> bool:
    """True when the observation's taxonomy contains ``target_taxon_id``.

    Shared by both search modes so they can never disagree about what is inside a
    genus. Both ancestor lists are type-checked because iNaturalist returns
    ``ancestor_ids`` on most records and the expanded ``ancestors`` objects on
    some, and a malformed one must cost a single observation rather than the
    whole batch.
    """
    taxon = observation.get("taxon") or {}
    if not isinstance(taxon, dict):
        return False
    target = str(target_taxon_id)
    if str(taxon.get("id")) == target:
        return True
    ancestor_ids = taxon.get("ancestor_ids") if isinstance(taxon.get("ancestor_ids"), list) else []
    if any(str(item) == target for item in ancestor_ids):
        return True
    ancestors = taxon.get("ancestors") if isinstance(taxon.get("ancestors"), list) else []
    return any(isinstance(item, dict) and str(item.get("id")) == target for item in ancestors)


def _observation_matches(observation: dict, mode: str, criteria: dict) -> bool:
    if mode == "project":
        return True
    if mode == "user":
        return str((observation.get("user") or {}).get("login") or "").casefold() == criteria["label"].casefold()
    return _taxon_ancestry_matches(observation, criteria["taxon_id"])


def _fetch_matches(ids: List[str], mode: str, criteria: dict,
                   deadline: Optional[float] = None) -> List[dict]:
    params = {"id": ",".join(ids), "per_page": len(ids)}
    if criteria.get("project_id"):
        params["project_id"] = criteria["project_id"]
    # ONE retry layer: `max_attempts` counts retries, so this is already two
    # HTTP attempts with a Retry-After backoff between them. The batch loop
    # used to wrap it in a second retry, and the comment here used to claim the
    # resulting four attempts could not "outlive the HTTP request window" --
    # which was the assumption that made the request unbounded, since each
    # attempt also paces and each backoff sleeps. The deadline is what bounds
    # it now, and `_http_request` enforces it around every one of those waits.
    data = _api_get("/observations", params, max_attempts=1, deadline=deadline)
    return [
        item for item in (data or {}).get("results", [])
        if isinstance(item, dict) and _observation_matches(item, mode, criteria)
    ]


def _format_place(places: List[dict]) -> str:
    choices = [
        place for place in places
        if isinstance(place, dict) and isinstance(place.get("admin_level"), int)
        and place["admin_level"] >= 0
    ]
    if not choices:
        return ""
    place = max(choices, key=lambda item: item["admin_level"])
    label = place.get("display_name") or place.get("name") or ""
    return " ".join(
        part.strip() for part in re.sub(r"\bUnited States\b", "US", re.sub(r"\bCounty\b", "Co.", label)).split(",")
        if part.strip()
    )


def _locations(observations: List[dict],
               deadline: Optional[float] = None) -> Dict[str, str]:
    """Resolve place names, giving up rather than overrunning a deadline.

    This runs while the RESPONSE is being assembled, after the search itself
    has stopped, and it is several more paced and retrying API calls. Left
    unbounded it could spend minutes past a deadline the search had already
    respected -- so when the time is gone the place lookups are skipped and
    every observation falls back to its own ``place_guess``. A slightly vaguer
    location is a far better answer than a request nginx has already hung up on.
    """
    ids = sorted({
        str(place_id)
        for item in observations
        for place_id in (
            item.get("place_ids") if isinstance(item.get("place_ids"), list) else []
        )
    })
    places: Dict[str, dict] = {}
    for start in range(0, len(ids), BATCH_SIZE):
        batch = ids[start:start + BATCH_SIZE]
        left = remaining_seconds(deadline)
        if left is not None and left <= MIN_INAT_REQUEST_TIMEOUT_SECONDS:
            break
        try:
            data = _api_get(f"/places/{','.join(batch)}", {"per_page": len(batch)},
                            deadline=deadline)
        except InatTreeError:
            continue
        for place in (data or {}).get("results", []):
            if isinstance(place, dict) and place.get("id") is not None:
                places[str(place["id"])] = place
    result = {}
    for observation in observations:
        place_ids = observation.get("place_ids") if isinstance(observation.get("place_ids"), list) else []
        resolved = [places[str(item)] for item in place_ids if str(item) in places]
        result[str(observation.get("id"))] = _format_place(resolved) or str(observation.get("place_guess") or "").strip() or "Unknown location"
    return result


def _serialize_observation(observation: dict, *, original_id: str, location: str) -> dict:
    taxon = observation.get("taxon") or {}
    user = observation.get("user") or {}
    photos = observation.get("photos") if isinstance(observation.get("photos"), list) else []
    photo_url = next((item.get("url") for item in photos if isinstance(item, dict) and item.get("url")), None)
    default_photo = taxon.get("default_photo") if isinstance(taxon.get("default_photo"), dict) else {}
    return {
        "id": observation.get("id"),
        "url": f"https://www.inaturalist.org/observations/{observation.get('id')}",
        "is_original": str(observation.get("id")) == original_id,
        "observed_on": observation.get("observed_on"),
        "location": location,
        "place_guess": observation.get("place_guess"),
        "photo_url": photo_url or default_photo.get("square_url"),
        "user": {"id": user.get("id"), "login": user.get("login")},
        "taxon": _taxon_summary(taxon),
    }


# --- Bounded auto mode -----------------------------------------------------
# A port of inat.finder.py 1.8.0's --auto ladder, bounded so it fits an HTTP
# request. The CLI can spend seven minutes on the widest stage of a nine-digit
# number; a synchronous endpoint cannot, and holding a Gunicorn slot that long
# would starve the rest of the site. So this runs until it finds a full match,
# runs out of ladder, hits a stage too large to start unasked, or spends its
# per-request budget - and in the last two cases it hands back a resume cursor
# instead of a timeout. The caller continues with another request, and because
# the cursor replays candidate generation offline, nothing is ever re-requested.


def _fetch_observations(
    ids: List[str],
    project_id: Optional[str] = None,
    deadline: Optional[float] = None,
) -> List[dict]:
    """Fetch observations by ID. Raises InatTreeError if the request fails.

    The ``deadline`` goes all the way down to ``_http_request``, which is the
    only layer that can honour it: pacing waits, retry sleeps and the socket
    timeout all happen there, and a timeout computed up here would bound none
    of them. ``max_attempts=1`` permits ONE retry (the value counts retries),
    so a call is at most two HTTP attempts and the caller adds no second retry
    layer of its own.
    """
    params = {"id": ",".join(ids), "per_page": len(ids)}
    if project_id:
        params["project_id"] = project_id
    data = _api_get("/observations", params, max_attempts=1, deadline=deadline)
    return [item for item in (data or {}).get("results", []) if isinstance(item, dict)]


def _fetch_project_membership(
    ids: List[str], project_id: str, deadline: Optional[float] = None
) -> Set[str]:
    """Return the subset of ``ids`` belonging to ``project_id``.

    Membership has to be answered by iNaturalist rather than read off the
    observation: a collection project's membership is rule-based and does not
    appear in an observation's own ``project_ids``.
    """
    return {
        str(item.get("id"))
        for item in _fetch_observations(ids, project_id=project_id, deadline=deadline)
        if item.get("id") is not None
    }


def _evaluate_clue(criterion: dict, observation: dict, member_ids: Optional[Set[str]]) -> str:
    """Return ``match``, ``no_match`` or ``unknown`` for one clue.

    ``unknown`` is the important one: project membership is answered by its own
    request, and when that request fails the genus, user and taxon evidence for
    the same observation is still perfectly good. Saying "unknown" keeps that
    evidence instead of discarding the batch, and it never counts toward a score,
    so an unconfirmed clue can never end the search early or fake a full match.
    """
    if criterion["kind"] == "project":
        if member_ids is None:
            return "unknown"
        return "match" if str(observation.get("id")) in member_ids else "no_match"
    if criterion["kind"] == "user":
        login = str((observation.get("user") or {}).get("login") or "")
        return "match" if login.casefold() == criterion["label"].casefold() else "no_match"
    return "match" if _taxon_ancestry_matches(observation, criterion["taxon_id"]) else "no_match"


def _score_observation(
    observation: dict, member_ids: Optional[Set[str]], criteria: List[dict]
) -> Tuple[List[str], List[str]]:
    """Return ``(matched_kinds, unknown_kinds)``.

    Scoring is any-of on purpose. Requiring every clue to agree would hide the
    real observation whenever one supplied element was itself wrong, which is the
    common case auto mode exists for; ranking by how many agreed keeps the best
    answer on top without throwing the near misses away.
    """
    matched, unknown = [], []
    for criterion in criteria:
        verdict = _evaluate_clue(criterion, observation, member_ids)
        if verdict == "match":
            matched.append(criterion["kind"])
        elif verdict == "unknown":
            unknown.append(criterion["kind"])
    return matched, unknown


def _is_full_match(matched: List[str], criteria: List[dict]) -> bool:
    """True when every clue agreed. An unknown clue can never make this true."""
    return bool(criteria) and len(matched) == len(criteria)


def _score_payload(matched: List[str], unknown: List[str], criteria: List[dict]) -> dict:
    """The one score shape the response uses, for a match and for the original alike."""
    return {
        "matched": matched,
        "unknown": unknown,
        "matched_count": len(matched),
        "unknown_count": len(unknown),
        "total": len(criteria),
        "is_full_match": _is_full_match(matched, criteria),
    }


def resolve_auto_criteria(clues: Dict[str, str],
                          deadline: Optional[float] = None) -> dict:
    """Verify each supplied clue, dropping the ones iNaturalist cannot resolve.

    This is the whole difference between the two modes. A single-criterion search
    has exactly one criterion and an unresolvable one is a fatal request error,
    exactly as before. An auto search may have several, and one that cannot be
    resolved is reported, dropped and left out of scoring while the rest carry
    on - on a foray the mistaken element is as often the genus as the number.

    What that does NOT change: malformed input is always fatal, and an
    :class:`InatTreeError` is always an outage rather than an unresolvable clue.
    A clue must never be discarded because iNaturalist was unreachable.
    """
    criteria: List[dict] = []
    unusable: List[dict] = []
    project_id_param: Optional[str] = None
    membership_project_id: Optional[str] = None

    for kind in AUTO_CLUE_KINDS:
        term = str(clues.get(kind) or "").strip()
        if not term:
            continue
        if len(term) > 200:
            raise FinderValidationError(
                f"`{kind}` may contain at most 200 characters.",
                details={"field": kind, "max_length": 200},
                malformed=True,
            )
        try:
            resolved = resolve_criteria(kind, term, deadline=deadline)
        except FinderValidationError as exc:
            if exc.malformed:
                raise
            unusable.append({
                "kind": kind,
                "value": term,
                "reason": str(exc),
                "candidates": (exc.details or {}).get("candidates"),
            })
            continue
        entry = {"kind": kind, "value": term, "label": resolved["label"]}
        if kind in ("genus", "family", "taxon"):
            entry["taxon_id"] = resolved["taxon_id"]
            entry["taxon"] = resolved.get("taxon")
        if kind == "project":
            entry["project_id"] = resolved["project_id"]
        criteria.append(entry)

    project = next((item for item in criteria if item["kind"] == "project"), None)
    if project is not None:
        # A project on its own can be answered by filtering the main request,
        # which is cheaper and correct for collection projects. Sharing the
        # search with another clue rules that out - the filter would hide every
        # observation the other clues might have matched - so membership moves to
        # a second request per batch. A clue that was SUPPLIED but turned out
        # unusable still counts here: the decision follows what was asked for,
        # not what survived verification.
        others_supplied = any(
            str(clues.get(kind) or "").strip()
            for kind in AUTO_CLUE_KINDS if kind != "project"
        )
        if len(criteria) == 1 and not others_supplied:
            project_id_param = project["project_id"]
        else:
            membership_project_id = project["project_id"]

    return {
        "criteria": criteria,
        "unusable": unusable,
        "project_id_param": project_id_param,
        "membership_project_id": membership_project_id,
    }


def _public_criteria(criteria: List[dict]) -> List[dict]:
    """The clue list as the response reports it."""
    return [
        {key: item[key] for key in ("kind", "value", "label", "taxon_id", "taxon") if key in item}
        for item in criteria
    ]


def find_observations_auto(
    *,
    observation: Any,
    clues: Dict[str, Any],
    digits_off: int = AUTO_DEFAULT_MAX_DIGITS,
    resume: Optional[str] = None,
    confirm: bool = False,
    budget: int = MAX_REQUEST_CANDIDATES,
    time_budget: Optional[float] = MAX_REQUEST_SECONDS,
) -> dict:
    """Climb the typo ladder until something matches, or a budget runs out.

    Two budgets bound one synchronous request: ``budget`` caps the candidates
    checked, and ``time_budget`` caps the seconds spent. Neither implies the
    other -- pacing, 429 retries, socket timeouts and the extra membership
    request mean a candidate count is not a duration -- so both are enforced,
    at batch boundaries, where the resume cursor is exact.
    """
    if isinstance(digits_off, bool) or not isinstance(digits_off, int) or not 1 <= digits_off <= 3:
        raise FinderValidationError(
            "`digits_off` must be an integer from 1 through 3.",
            details={"field": "digits_off", "minimum": 1, "maximum": 3},
            malformed=True,
        )
    # Started before clue resolution, which is itself several iNaturalist calls.
    seconds_allowed = float(time_budget) if time_budget else None
    deadline = (time.monotonic() + seconds_allowed) if seconds_allowed else None
    observation_id = parse_observation_id(observation)
    normalized = {kind: str(clues.get(kind) or "").strip() for kind in AUTO_CLUE_KINDS}
    resolved = resolve_auto_criteria(normalized, deadline=deadline)
    criteria = resolved["criteria"]
    project_id_param = resolved["project_id_param"]
    membership_project_id = resolved["membership_project_id"]
    fingerprint = search_fingerprint(observation_id, criteria, digits_off)

    seen: Set[str] = set()
    start_stage = 1
    resuming = False
    if resume:
        stage_index, offset, token_fingerprint = parse_resume_token(resume)
        if token_fingerprint != fingerprint:
            raise FinderValidationError(
                "`resume` belongs to a different search. A cursor is bound to the "
                "observation number, the clues and `digits_off`, so it can never be "
                "replayed against a search it did not come from.",
                details={"field": "resume"},
                malformed=True,
            )
        if not 1 <= stage_index <= digits_off:
            raise FinderValidationError(
                f"`resume` points at stage {stage_index}, which this search does not "
                f"have (1 to {digits_off}). Raise `digits_off` to search further.",
                details={"field": "resume"},
                malformed=True,
            )
        resume_plan_total = build_candidate_plan(observation_id, stage_index).total
        if not 0 <= offset <= resume_plan_total:
            raise FinderValidationError(
                f"`resume` points {offset} candidate(s) into stage {stage_index}, "
                f"which only has {resume_plan_total}.",
                details={"field": "resume"},
                malformed=True,
            )
        seen = restore_seen_ids(observation_id, stage_index, offset)
        start_stage = stage_index
        resuming = True

    matches: List[dict] = []
    stages: List[dict] = []
    notices: List[str] = []
    checked = 0
    unchecked = 0
    failed_batches = 0
    membership_unknown = 0
    original: Optional[dict] = None
    original_score: Optional[dict] = None
    stop_reason = "exhausted"
    next_stage: Optional[dict] = None
    cursor: Optional[dict] = None

    def make_cursor(stage_index: int, offset: int) -> Optional[dict]:
        """A cursor, but only when nothing was left unchecked.

        A failed batch's IDs are already in ``seen``, so a cursor issued after a
        failure would skip them for good and let a later call report a clean "no
        match" over the gap. Such a run says it is incomplete instead.
        """
        if unchecked or membership_unknown or stage_index > digits_off:
            return None
        return {
            "token": build_resume_token(stage_index, offset, fingerprint),
            "stage": stage_index,
            "offset": offset,
        }

    def finish(status: str, reason: str, message: Optional[str] = None) -> dict:
        """Assemble the result, never letting a failure hide behind a clean status."""
        final_status, final_reason, final_message = status, reason, message
        if status not in ("error",) and (unchecked or membership_unknown):
            final_status, final_reason = "incomplete", "failures"
            if membership_unknown and not final_message:
                final_message = (
                    "Project membership could not be checked for part of this search, "
                    "so the project clue is unknown for some results."
                )
        ranked = _rank_auto_matches(matches, observation_id, criteria,
                                    deadline=deadline)
        complete = final_status in ("match_found", "no_match") and final_reason not in (
            "declined", "large_stage", "budget_exhausted", "deadline_exhausted",
            "too_large",
        )
        return {
            "query": {
                "observation_id": observation_id,
                "mode": "auto",
                "digits_off": digits_off,
                "clues": {k: v for k, v in normalized.items() if v},
                "resumed": resuming,
            },
            "status": final_status,
            "stop_reason": final_reason,
            "message": final_message,
            "complete": complete,
            "criteria": _public_criteria(criteria),
            "unusable_clues": resolved["unusable"],
            # Things the caller should say out loud but that are not errors -
            # today, that one clue could not separate several neighbours.
            "notices": notices,
            "original": original,
            "original_score": original_score,
            "original_checked": original is not None or resuming,
            "matches": ranked,
            "match_count": len(ranked),
            "full_match_count": sum(1 for item in ranked if item["score"]["is_full_match"]),
            "checked_variations": checked,
            "unchecked_variations": unchecked,
            "failed_batches": failed_batches,
            "stages": stages,
            "resume": cursor,
            "next_stage": next_stage,
        }

    def record(observation_record: dict, matched: List[str], unknown: List[str], stage_index: int):
        matches.append({
            "observation": observation_record,
            "matched": matched,
            "unknown": unknown,
            "stage": stage_index,
        })

    # Stage 0 asks "what is this number?" and is deliberately never filtered by
    # project: a project filter answers a different question and would hide a
    # real observation that simply is not a member, reporting it as nonexistent.
    if not resuming:
        try:
            found = _fetch_observations([observation_id], deadline=deadline)
        except InatTreeError:
            # An outage is not a fact about the observation.
            unchecked += 1
            failed_batches += 1
            stages.append({"stage": 0, "total": 1, "attempted": 0, "unchecked": 1})
            found = []
        else:
            stages.append({"stage": 0, "total": 1, "attempted": 1, "unchecked": 0})
        if found:
            original = found[0]
            member_ids: Optional[Set[str]] = None
            project_id = project_id_param or membership_project_id
            if project_id:
                try:
                    member_ids = _fetch_project_membership(
                        [observation_id], project_id, deadline=deadline
                    )
                except InatTreeError:
                    # Stage 0 is never repeated on a resume, so an unanswered
                    # question here would be skipped for good.
                    membership_unknown += 1
            matched, unknown = _score_observation(original, member_ids, criteria)
            original_score = _score_payload(matched, unknown, criteria)
            if matched:
                record(original, matched, unknown, 0)
            if _is_full_match(matched, criteria):
                return finish("match_found", "full_match")

    if not criteria:
        # Nothing to filter on. Enumerating thousands of neighbouring IDs would
        # return every observation that happens to exist near this number, which
        # is noise rather than an answer.
        return finish(
            "no_match",
            "no_clues",
            "No usable clue was supplied, so there was nothing to search for beyond "
            "the number itself. Add genus, family, taxon, user or project to widen "
            "the search to nearby numbers.",
        )

    for index in range(start_stage, digits_off + 1):
        plan = build_candidate_plan(observation_id, index)
        stage = AutoStage(index, plan, seen)
        if stage.total <= 0:
            continue
        seconds = estimate_stage_seconds(stage.total, membership_project_id)
        stage_summary = {
            "stage": index,
            "label": stage.label,
            "estimated_candidates": stage.total,
            "estimated_seconds": seconds,
        }
        if stage.total > LARGE_SEARCH_THRESHOLD and not confirm:
            # Never start a stage this size unasked. It was not searched, so the
            # call has established nothing about it and must not say "no match".
            next_stage = stage_summary
            cursor = make_cursor(index, 0)
            return finish(
                "needs_confirmation",
                "large_stage",
                f"Stage {index} would check {stage.total:,} more observation numbers "
                f"(about {seconds}s). Repeat the request with `confirm: true` and this "
                "`resume` cursor to continue.",
            )

        # Enough time for at least one batch, or do not start the stage at all:
        # a cursor at offset 0 is exact, and nothing here has been checked.
        stage_reserve = BATCH_TIME_RESERVE_SECONDS + (
            MEMBERSHIP_TIME_RESERVE_SECONDS if membership_project_id else 0.0
        )
        remaining = remaining_seconds(deadline)
        if remaining is not None and remaining < stage_reserve:
            next_stage = stage_summary
            cursor = make_cursor(index, 0)
            return finish(
                "needs_confirmation",
                "deadline_exhausted",
                f"This request reached its {seconds_allowed:.0f}s time limit "
                f"before stage {index} could be started. Repeat the request with "
                "the `resume` cursor to continue from here.",
            )

        consecutive_failures = 0
        found_full = False
        budget_exhausted = False
        deadline_exhausted = False
        iterator = iter(stage)

        def take(size: int) -> List[str]:
            batch = []
            for candidate in iterator:
                batch.append(candidate)
                if len(batch) >= size:
                    break
            return batch

        stage_checked = 0
        stage_unchecked = 0
        while True:
            if checked >= budget:
                budget_exhausted = True
                break
            remaining = remaining_seconds(deadline)
            if remaining is not None and remaining < stage_reserve:
                # Stop between batches, where the plan position is exact and a
                # cursor resumes without re-requesting or skipping anything.
                deadline_exhausted = True
                break
            batch = take(min(BATCH_SIZE, budget - checked))
            if not batch:
                break
            # ONE retry layer. _fetch_observations already permits a retry
            # inside _http_request, where the backoff honours iNaturalist's own
            # Retry-After and the deadline is re-checked before each sleep and
            # each attempt. Wrapping it in a second loop here made a single
            # batch up to four HTTP attempts with two unbounded backoffs, which
            # no reserve computed from "two attempts" could ever have covered.
            results = None
            try:
                results = _fetch_observations(
                    batch, project_id=project_id_param, deadline=deadline
                )
            except InatDeadlineExceeded:
                # Out of time rather than an outage. The batch is unchecked
                # either way, which is what make_cursor() refuses to skip over.
                deadline_exhausted = True
            except InatTreeError:
                pass
            if results is None:
                unchecked += len(batch)
                stage_unchecked += len(batch)
                failed_batches += 1
                consecutive_failures += 1
                if deadline_exhausted:
                    # No point trying the next batch: there is no time for it,
                    # and every further attempt would only add unchecked IDs.
                    skipped = max(0, stage.total - stage_checked - stage_unchecked)
                    unchecked += skipped
                    stage_unchecked += skipped
                    break
                if consecutive_failures >= MAX_CONSECUTIVE_FAILED_BATCHES:
                    # A sustained outage stops the stage rather than grinding
                    # through every remaining batch to fail at each one.
                    skipped = max(0, stage.total - stage_checked - stage_unchecked)
                    unchecked += skipped
                    stage_unchecked += skipped
                    break
                continue
            consecutive_failures = 0
            checked += len(batch)
            stage_checked += len(batch)

            member_ids = None
            if project_id_param:
                # The request was already filtered server-side, so everything it
                # returned is a member and nothing else in the batch is.
                member_ids = {str(item.get("id")) for item in results if item.get("id") is not None}
            elif membership_project_id and results:
                try:
                    member_ids = _fetch_project_membership(
                        batch, membership_project_id, deadline=deadline
                    )
                except InatDeadlineExceeded:
                    # Never guess membership: an unanswered clue is "unknown",
                    # exactly as a failed one is, and that also stops a cursor
                    # being issued over it.
                    membership_unknown += 1
                    deadline_exhausted = True
                except InatTreeError:
                    # The observations came back fine. Keep that evidence and
                    # mark only the project clue unknown for this batch.
                    membership_unknown += 1

            for item in results:
                matched, unknown = _score_observation(item, member_ids, criteria)
                if not matched:
                    continue
                record(item, matched, unknown, index)
                if _is_full_match(matched, criteria):
                    found_full = True
            if found_full and stage.total > EARLY_STOP_MIN_CANDIDATES:
                # Only a stage too big to finish aborts on a hit: there, checking
                # the rest costs minutes. A smaller stage runs to the end so every
                # equally good candidate is collected and ranked together.
                break

        stages.append({
            "stage": index,
            "total": stage.total,
            "attempted": stage_checked,
            "unchecked": stage_unchecked,
        })

        if found_full:
            stop_reason = "full_match"
            full_here = [
                match for match in matches
                if match["stage"] == index and _is_full_match(match["matched"], criteria)
            ]
            if len(full_here) > 1 and len(criteria) == 1:
                # Worth saying plainly rather than leaving the caller to infer it
                # from a list: one clue cannot separate these, and nearby numbers
                # share an uploader far more often than chance.
                notices.append(
                    f"{len(full_here)} nearby observations all match the only clue you gave "
                    f"({criteria[0]['kind']}), so the one listed first is a best guess rather "
                    "than an answer. iNaturalist numbers observations in the order they were "
                    "uploaded, so numbers next to each other often belong to the same person "
                    "and the same taxon. Check all of them, or add a second clue."
                )
            cursor = (make_cursor(index + 1, 0) if stage.exhausted()
                      else make_cursor(index, stage.plan_position))
            break
        if budget_exhausted:
            cursor = make_cursor(index, stage.plan_position)
            next_stage = {
                "stage": index,
                "label": stage.label,
                "estimated_candidates": max(0, stage.total - stage_checked - stage_unchecked),
                "estimated_seconds": estimate_stage_seconds(
                    max(0, stage.total - stage_checked - stage_unchecked), membership_project_id
                ),
            }
            return finish(
                "needs_confirmation",
                "budget_exhausted",
                f"This request checked its limit of {budget:,} observation numbers. "
                "Repeat the request with the `resume` cursor to continue from here.",
            )
        if deadline_exhausted:
            # Same shape as the candidate budget: a paused search, not a failed
            # one, and never a "no match" over numbers nobody looked at.
            cursor = make_cursor(index, stage.plan_position)
            next_stage = {
                "stage": index,
                "label": stage.label,
                "estimated_candidates": max(0, stage.total - stage_checked - stage_unchecked),
                "estimated_seconds": estimate_stage_seconds(
                    max(0, stage.total - stage_checked - stage_unchecked), membership_project_id
                ),
            }
            return finish(
                "needs_confirmation",
                "deadline_exhausted",
                f"This request reached its {seconds_allowed:.0f}s time limit after "
                f"checking {checked:,} observation number(s). Repeat the request "
                "with the `resume` cursor to continue from here.",
            )

    if matches:
        return finish("match_found", stop_reason)
    return finish("no_match", stop_reason)


def _rank_auto_matches(matches: List[dict], original_id: str, criteria: List[dict],
                       deadline: Optional[float] = None) -> List[dict]:
    """Deduplicate by observation ID and sort best-first.

    An observation can be scored more than once - the number as supplied may also
    turn up as a candidate - so the highest-scoring copy wins.

    Score decides the order first. Everything after it exists because equal scores
    are the common case rather than the rare one: with a single clue every hit
    scores 1 of 1, so without a tie-break the "best" match would be whichever
    candidate happened to have the lowest number. Ties therefore break on the
    stage that found the match (fewer digits off is a likelier typo), then on how
    far the number is from the one supplied, and only then on the number itself,
    which keeps the order stable between calls - something a caller diffing pages
    depends on. The supplied number, when it matched, sorts first: it is stage 0
    and its distance is zero.
    """
    best: Dict[str, dict] = {}
    for match in matches:
        key = str(match["observation"].get("id"))
        current = best.get(key)
        if current is None or len(match["matched"]) > len(current["matched"]):
            best[key] = match

    try:
        origin = int(original_id)
    except (TypeError, ValueError):
        origin = None

    def sort_key(match: dict):
        obs_id = int(match["observation"].get("id") or 0)
        stage = match["stage"] if match["stage"] is not None else float("inf")
        distance = abs(obs_id - origin) if origin is not None else 0
        return (-len(match["matched"]), stage, distance, obs_id)

    ordered = sorted(best.values(), key=sort_key)
    locations = _locations([match["observation"] for match in ordered],
                           deadline=deadline)
    serialized = []
    for match in ordered:
        item = match["observation"]
        payload = _serialize_observation(
            item,
            original_id=original_id,
            location=locations.get(str(item.get("id")), "Unknown location"),
        )
        payload["stage"] = match["stage"]
        payload["score"] = _score_payload(match["matched"], match["unknown"], criteria)
        serialized.append(payload)
    return serialized


def find_observations(*, observation: Any, mode: str, term: Any, digits_off: int = 1,
                      time_budget: Optional[float] = MAX_REQUEST_SECONDS) -> dict:
    """Find likely iNaturalist observations for a mistyped observation ID.

    Bounded by the same wall clock as the automatic search. It has no resume
    cursor, so reaching the deadline is reported through the `complete` and
    `unchecked_variations` fields this mode has always carried.
    """
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in VALID_MODES:
        raise FinderValidationError(
            f"`mode` must be one of: {', '.join(sorted(VALID_MODES))}.",
            details={"field": "mode", "allowed": sorted(VALID_MODES)},
        )
    normalized_term = str(term or "").strip()
    if not normalized_term or len(normalized_term) > 200:
        raise FinderValidationError(
            "`term` is required and may contain at most 200 characters.",
            details={"field": "term", "max_length": 200},
        )
    if isinstance(digits_off, bool) or not isinstance(digits_off, int) or not 1 <= digits_off <= 3:
        raise FinderValidationError(
            "`digits_off` must be an integer from 1 through 3.",
            details={"field": "digits_off", "minimum": 1, "maximum": 3},
        )

    deadline = (time.monotonic() + float(time_budget)) if time_budget else None
    observation_id = parse_observation_id(observation)
    variations = build_variations(observation_id, digits_off)
    criteria = resolve_criteria(normalized_mode, normalized_term, deadline=deadline)

    found: Dict[str, dict] = {}
    original = _fetch_matches([observation_id], normalized_mode, criteria,
                              deadline=deadline)
    for item in original:
        found[str(item.get("id"))] = item

    checked = 0
    unchecked = 0
    failed_batches = 0
    consecutive_failures = 0
    for start in range(0, len(variations), BATCH_SIZE):
        batch = variations[start:start + BATCH_SIZE]
        # This mode has no resume cursor, but it does report `complete` and
        # `unchecked_variations`, so running out of time is expressed there
        # rather than by overrunning the request. MAX_API_VARIATIONS caps the
        # candidate COUNT at 10,000, which is still 50 batches -- each one
        # paced, and each able to retry -- so like the auto ladder this needed
        # a bound on the clock rather than only on the work.
        left = remaining_seconds(deadline)
        if left is not None and left < BATCH_TIME_RESERVE_SECONDS:
            unchecked += len(variations) - start
            break
        matches = None
        try:
            matches = _fetch_matches(batch, normalized_mode, criteria,
                                     deadline=deadline)
        except InatDeadlineExceeded:
            unchecked += len(variations) - start
            break
        except InatTreeError:
            pass
        if matches is None:
            unchecked += len(batch)
            failed_batches += 1
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILED_BATCHES:
                unchecked += len(variations) - start - len(batch)
                break
            continue
        consecutive_failures = 0
        checked += len(batch)
        for item in matches:
            found[str(item.get("id"))] = item

    observations = list(found.values())
    locations = _locations(observations, deadline=deadline)
    matches = [
        _serialize_observation(item, original_id=observation_id,
                               location=locations.get(str(item.get("id")), "Unknown location"))
        for item in observations
    ]
    matches.sort(key=lambda item: (not item["is_original"], int(item["id"])))
    return {
        "query": {
            "observation_id": observation_id,
            "mode": normalized_mode,
            "term": normalized_term,
            "digits_off": digits_off,
        },
        "criteria": criteria,
        "matches": matches,
        "match_count": len(matches),
        "checked_variations": checked,
        "unchecked_variations": unchecked,
        "total_variations": len(variations),
        "original_checked": True,
        "complete": unchecked == 0,
        "failed_batches": failed_batches,
    }
