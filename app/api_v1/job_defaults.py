"""Job-parameter defaults applied by POST /api/v1/jobs.

These live here rather than inline in `routes.py` so `openapi.py` can publish
the same values the handler actually applies. Anything that has a Config
setting behind it is read from Config directly at the point of use; what is
defined here is the handful of API-level defaults that have no Config knob.

`bootstrap` deliberately does not use `Config.DEFAULT_BOOTSTRAPS` (100): the
public API has always defaulted to 1000 replicates, and quietly dropping a
caller's support values by an order of magnitude is not a documentation fix.
"""

DEFAULT_TREE_METHOD = "fasttree"
DEFAULT_ALIGNMENT_METHOD = "mafft"
DEFAULT_BOOTSTRAP = 1000
