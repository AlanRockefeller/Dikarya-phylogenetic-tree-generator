"""Site navigation: one definition of the global bar, local nav and footer.

Both layouts (``journal/base_journal.html`` and ``base_modern.html``) render
the shell from this module through the ``partials/site_*.html`` templates, so a
new tool or page is added here once rather than in each header and footer.

Items name Flask endpoints, not URLs. An item whose endpoint is not registered
falls back to its ``fallback`` endpoint, or is left out, so the config can name
pages that land in a later release (``main.workbench``, ``journal.authors``)
without breaking the pages that render it today.
"""

from datetime import datetime, timedelta, timezone

from flask import current_app, g, request, url_for


SECTIONS = (
    {'key': 'journal', 'label': 'Journal', 'endpoint': 'journal.home'},
    {'key': 'workbench', 'label': 'Workbench', 'endpoint': 'main.workbench',
     'fallback': 'main.sequence_entry'},
)

# ``also`` lists other endpoints that mark the item active: the job pages
# belong to Tree Builder, for example. Journal home has no tab on purpose --
# the logo and the Journal switcher item cover it.
JOURNAL_NAV = (
    {'label': 'Current Issue', 'endpoint': 'journal.current_issue'},
    {'label': 'Archive', 'endpoint': 'journal.archive'},
    {'label': 'Taxonomy', 'endpoint': 'journal.taxonomy'},
    {'label': 'Submit', 'endpoint': 'journal.submit_manuscript'},
    {'label': 'For Authors', 'endpoint': 'journal.authors', 'fallback': 'journal.resources',
     'also': ('journal.resources', 'journal.ai_policy')},
    {'label': 'For Reviewers', 'endpoint': 'journal.reviewers'},
    {'label': 'About', 'endpoint': 'journal.about',
     'also': ('journal.contact', 'journal.support')},
)

WORKBENCH_NAV = (
    {'label': 'Overview', 'endpoint': 'main.workbench'},
    {'label': 'Tree Builder', 'endpoint': 'main.sequence_entry',
     'also': ('main.job_status', 'main.job_viewer')},
    {'label': 'iNat Finder', 'endpoint': 'main.inat_finder'},
    {'label': 'Voucher Sync', 'endpoint': 'main.voucher_sync'},
    {'label': 'Voucher Labels', 'endpoint': 'main.voucher_labels'},
    {'label': 'Docs', 'endpoint': 'main.help_page'},
)

LOCAL_NAV = {'journal': JOURNAL_NAV, 'workbench': WORKBENCH_NAV}

FOOTER_COLUMNS = (
    {'heading': 'Journal', 'links': (
        {'label': 'Current Issue', 'endpoint': 'journal.current_issue'},
        {'label': 'Archive', 'endpoint': 'journal.archive'},
        {'label': 'Taxonomy', 'endpoint': 'journal.taxonomy'},
    )},
    {'heading': 'Authors', 'links': (
        {'label': 'Submit Manuscript', 'endpoint': 'journal.submit_manuscript'},
        {'label': 'Author Guidelines', 'endpoint': 'journal.submit_manuscript', 'anchor': 'guidelines'},
        {'label': 'AI Policy', 'endpoint': 'journal.ai_policy'},
        {'label': 'Writing Resources', 'endpoint': 'journal.resources'},
    )},
    {'heading': 'Reviewers', 'links': (
        {'label': 'Reviewer Guidelines', 'endpoint': 'journal.reviewers'},
        {'label': 'Reviewer Portal', 'endpoint': 'journal.reviewers', 'anchor': 'portal'},
    )},
    {'heading': 'Workbench', 'links': (
        {'label': 'Tree Builder', 'endpoint': 'main.sequence_entry'},
        {'label': 'iNat Finder', 'endpoint': 'main.inat_finder'},
        {'label': 'Voucher Sync', 'endpoint': 'main.voucher_sync'},
        {'label': 'Voucher Labels', 'endpoint': 'main.voucher_labels'},
        {'label': 'Docs', 'endpoint': 'main.help_page'},
    )},
    {'heading': 'Dikarya', 'links': (
        {'label': 'Mission & Scope', 'endpoint': 'journal.about'},
        {'label': 'Editorial Board', 'endpoint': 'journal.about', 'anchor': 'editorial-board'},
        {'label': 'Publication Ethics', 'endpoint': 'journal.about', 'anchor': 'ethics'},
        {'label': 'Contact Us', 'endpoint': 'journal.contact'},
        {'label': 'Support the Journal', 'endpoint': 'journal.support'},
        {'label': "What's New", 'endpoint': 'main.whats_new'},
        {'label': 'Feedback & Roadmap', 'endpoint': 'main.todo'},
    )},
)

COPYRIGHT = '© 2026 Dikarya Project.'
LICENSE = {'label': 'CC BY 4.0.', 'url': 'https://creativecommons.org/licenses/by/4.0/'}

ACTIVE_JOB_STATUSES = ('queued', 'running')
# A job abandoned in "running" (a killed worker; see `flask reap-stuck-jobs`)
# would otherwise hold the My Jobs badge up forever.
ACTIVE_JOB_MAX_AGE = timedelta(days=7)


def _resolve(item):
    """Return (endpoint, url) for an item, or (None, None) when neither its
    endpoint nor its fallback is registered."""
    for endpoint in (item['endpoint'], item.get('fallback')):
        if endpoint and endpoint in current_app.view_functions:
            url = url_for(endpoint)
            if item.get('anchor'):
                url = f"{url}#{item['anchor']}"
            return endpoint, url
    return None, None


def _current(item, resolved_endpoint, endpoint):
    """aria-current value: "page" on the item's own page, "true" on a page it
    owns through ``also`` (or a fallback), None otherwise."""
    if endpoint is None:
        return None
    if endpoint == item['endpoint'] or endpoint == resolved_endpoint:
        return 'page'
    if endpoint in item.get('also', ()):
        return 'true'
    return None


def active_section():
    return 'journal' if request.blueprint == 'journal' else 'workbench'


def _links(items, endpoint=None):
    links = []
    for item in items:
        resolved, url = _resolve(item)
        if url is None:
            continue
        links.append({'label': item['label'], 'url': url,
                      'current': _current(item, resolved, endpoint)})
    return links


def active_job_count(user):
    """Queued or running jobs for the signed-in user; 0 on any failure."""
    if not getattr(user, 'is_authenticated', False):
        return 0
    try:
        from app.models import Job
        # Job.created_at is naive UTC.
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - ACTIVE_JOB_MAX_AGE
        return (Job.query.filter(Job.user_id == user.id,
                                 Job.status.in_(ACTIVE_JOB_STATUSES),
                                 Job.deleted_at.is_(None),
                                 Job.created_at >= cutoff)
                .count())
    except Exception as exc:
        # Same reasoning as the What's New badge: a failed query leaves the
        # session needing a rollback, which would break the page around it.
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:
            pass
        from app.services.log_context import log_degradation_rate_limited
        log_degradation_rate_limited(
            current_app.logger, "active_job_badge_unavailable",
            "My Jobs badge suppressed because its lookup failed",
            exception=type(exc).__name__,
        )
        return 0


def site_nav():
    """Everything the shell partials render, built once per request.

    Exposed to templates as a callable rather than a value so that pages which
    do not render the shell never pay for the job-count query.
    """
    cached = getattr(g, '_site_nav', None)
    if cached is not None:
        return cached

    from flask_login import current_user

    endpoint = request.endpoint
    section = active_section()
    sections = []
    for item in SECTIONS:
        resolved, url = _resolve(item)
        current = None
        if item['key'] == section:
            current = 'page' if endpoint in (item['endpoint'], resolved) else 'true'
        sections.append({'key': item['key'], 'label': item['label'], 'url': url,
                         'current': current})

    def url(name):
        return url_for(name) if name in current_app.view_functions else None

    nav = {
        'section': section,
        'section_label': next(s['label'] for s in SECTIONS if s['key'] == section),
        'home_url': url('journal.home'),
        'sections': sections,
        'local': _links(LOCAL_NAV[section], endpoint),
        'footer': [{'heading': col['heading'], 'links': _links(col['links'])}
                   for col in FOOTER_COLUMNS],
        'copyright': COPYRIGHT,
        'license': LICENSE,
        'changelog_url': url('main.whats_new'),
        'roadmap_url': url('main.todo'),
        'help_url': url('main.help_page'),
        'jobs_url': url('user.user_jobs'),
        'login_url': url('auth.login'),
        'register_url': url('auth.register'),
        'logout_url': url('auth.logout'),
        'active_job_count': active_job_count(current_user),
    }
    g._site_nav = nav
    return nav
