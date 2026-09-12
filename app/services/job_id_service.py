"""Minting of job ids.

Job ids used to be UUID4 strings, which made every job URL 36 characters of
hyphenated hex. New jobs get a short base36 id instead (``/job/aq7c/view``);
the ~11.6k UUID jobs already on disk keep working because
``validate_job_id()`` accepts both shapes.

Ids are not treated as secrets -- a 4-character id is guessable and that is an
accepted trade for short links (see the job-id conventions in AGENTS.md). The
length is adaptive anyway, but for collision headroom rather than for secrecy:
minting widens on its own once a length gets crowded, so the ids stay short
while the corpus is small and grow with it. See MIN_LENGTH/ATTEMPTS_PER_LENGTH.
"""

import logging
import secrets

from app.config import Config

logger = logging.getLogger(__name__)

ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

# Shortest id we will mint. Widening is automatic (see generate_job_id).
MIN_LENGTH = 4
MAX_LENGTH = 12

# How many random candidates to try at a given length before concluding the
# space is too crowded and moving up one character. Density has to be high for
# this to trip: at 12 tries, widening happens once roughly half the space is
# taken, so 4-character ids last until ~800k jobs exist.
ATTEMPTS_PER_LENGTH = 12

# Ids that would read as something other than an id, or collide with a path
# segment if a literal route is ever added under /job/. Cheaper to skip at mint
# time than to explain later.
RESERVED_JOB_IDS = frozenset({
    "view", "edit", "new", "news", "list", "null", "none", "test", "json",
    "file", "tree", "logs", "help", "user", "jobs", "true", "fals", "root",
    "admin", "api", "www", "cunt", "fuck", "shit", "piss", "twat", "rape",
    "damn", "slut", "cock", "dick", "anus", "turd", "wank", "arse",
})


def _candidate(length):
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def generate_job_id():
    """Return a short job id that is free in both the database and var/jobs.

    Checks both because the two can disagree: a job directory outlives a
    deleted database row, and reusing that id would hand the new job the old
    one's artifacts.
    """
    from app.models import Job

    for length in range(MIN_LENGTH, MAX_LENGTH + 1):
        candidates = []
        while len(candidates) < ATTEMPTS_PER_LENGTH:
            cand = _candidate(length)
            if cand not in RESERVED_JOB_IDS and cand not in candidates:
                candidates.append(cand)

        # One query for the whole batch rather than one per candidate; at low
        # density the first candidate is almost always free.
        #
        # Best-effort on purpose: minting happens in the request that is about
        # to INSERT the row, and that INSERT is what actually enforces
        # uniqueness. If the session cannot answer here we still have the
        # directory check below and the primary key behind it, so a
        # non-functional query must not be the thing that stops a submission.
        taken = set()
        try:
            from app.extensions import db
            taken = {
                row[0] for row in
                db.session.query(Job.id).filter(Job.id.in_(candidates)).all()
            }
        except Exception:
            logger.debug("job id mint: database not queryable, "
                         "falling back to the on-disk check", exc_info=True)
        for cand in candidates:
            if cand in taken:
                continue
            if (Config.JOB_DIR / cand).exists():
                continue
            return cand

    # Every length up to MAX_LENGTH was crowded, which cannot happen short of
    # an astronomically large corpus or a broken RNG. Failing loudly beats
    # returning an id that may already own someone else's artifacts.
    raise RuntimeError(
        "could not mint a free job id up to %d characters" % MAX_LENGTH
    )
