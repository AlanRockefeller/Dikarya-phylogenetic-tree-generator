"""Separate internet-wide scanner junk from probes actually aimed at Dikarya.

Alan 9/12/26 - 21,968 of 34,263 requests in a representative week were 4xx, and
almost all of them were vulnerability sweeps: `/.env`, `/.git/config`,
`/containers/json`, `POST /hello.world`. That volume is what made `access.log`
big enough for logrotate's old `rotate 14` to bite, and it buried the handful of
requests that were genuinely interested in *this* application.

The two are worth different treatment, so they get different destinations:

* **scanner** -- an internet-wide probe for software Dikarya does not run and
  never has. It cannot succeed here. Logged to `var/logs/scanner.log`, which no
  digest section and no `errors.log` reader has to wade through. Kept rather
  than dropped: a sweep is still evidence when an IP later does something
  targeted, and the file compresses to almost nothing.
* **targeted** -- someone learning how *this* app is put together: path
  traversal against the job artifact routes, malformed job ids, probing under
  `/api/` or `/admin/`. Logged at WARNING as `event=security.suspicious`, so it
  lands in `errors.log` beside everything else that is actually broken.

Ordinary user 4xx (a mistyped job URL, an expired CSRF token) is neither, and
keeps going exactly where it already went.

Classification order matters: the targeted checks run first, so a traversal
attempt buried in a path that also ends in `.php` is reported as targeted rather
than filed away as scanner noise.

This module is deliberately free of Flask and of any import side effects, so
``scripts/dikarya_log_digest.py`` can import the same lists rather than keeping
a second copy that drifts.
"""

import re
from urllib.parse import unquote

# --------------------------------------------------------------------------
# Internet-wide scanner surface
# --------------------------------------------------------------------------

STATIC_SUFFIXES = (
    ".css", ".js", ".map", ".ico", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".webp", ".woff", ".woff2",
)

SCANNER_MARKERS = (
    "/.env", "/wp-", "/wordpress", "/phpmyadmin", "/xmlrpc", "/cgi-bin", "/.git",
    "/vendor/php", "/actuator", "/.aws", "/.ssh", "/.svn", "/.hg", "/.docker",
    "/.vscode", "/.idea", "/.well-known/security", "/config.json", "/credentials",
    "/id_rsa", "/backup.sql", "/dump.sql", "/database.sql", "/server-status",
    "/solr/", "/jenkins", "/hudson", "/manager/html", "/struts", "/login.action",
    "/telescope", "/debug/default", "/geoserver", "/owa/", "/autodiscover",
    "/boaform", "/hnap1", "/setup.cgi", "/shell", "/eval-stdin", "/wp/",
    # Appliance / webmail credential probes seen daily against this host.
    "/+cscoe+", "/remote/login", "/dana-na", "/global-protect", "/ecp/",
    "/onvif", "/device_service", "/mcp",
)

# Exact paths that only a scanner asks for. Exact matches, not prefixes: Dikarya
# has real routes that merely start with some of these words.
SCANNER_EXACT_PATHS = frozenset({
    "/login", "/logon", "/signin", "/ip", "/sse", "/graphql", "/api/graphql",
    "/config", "/env", "/settings", "/api/config", "/api/env", "/api/settings",
    "/api/v1/config", "/api/v1/env", "/api/v1/settings", "/server-info",
    "/console", "/status", "/info",
    # Auth/console routes this app has never had. One scanner probed each of
    # these 88 times in a day, in the same sweep as the /login and /signin
    # probes above, but they were landing in the product bucket and crowding
    # out the real 4xx entries. Dikarya's own auth lives at /auth/login.
    "/signup", "/register", "/dashboard", "/admin", "/account",
    "/auth/callback", "/api/auth/signin", "/login.html", "/sftp-config.json",
    # Generic fetch/proxy/config endpoints from a burst scanner that rotated
    # dozens of fake crawler user agents. Dikarya has never exposed these exact
    # routes; real downloads and previews live under scoped resource paths.
    "/fetch", "/proxy", "/api/proxy", "/api/v1/fetch", "/api/download",
    "/api/image", "/api/preview", "/api/v2/settings", "/api/v2/config",
    # Historical credential/PHP probes predate the explicit limiter noise tag.
    "/phpinfo", "/_profiler/phpinfo", "/_environment",
    "/webroot/index.php/_environment", "/phpinfo.php.old", "/phpinfo.php~",
    "/phpinfo.php.save", "/application_default_credentials.json", "/key.json",
    "/service-account.json", "/sa.json", "/gcp-key.json", "/gcp-credentials.json",
    "/gcp-sa.json", "/google-credentials.json", "/google-key.json",
    "/.config/gcloud/application_default_credentials.json", "/keyfile.json",
    "/firebase-adminsdk.json", "/firebase-key.json",
    # Alan 9/12/26 - Seen in the current week's sweeps.
    "/todo", "/blog", "/containers/json", "/hello.world", "/test.hello",
    "/v2/_catalog", "/debug", "/metrics",
})

SCRIPT_EXT_RE = re.compile(r"\.(?:php|asp|aspx|jsp|cgi|pl|cfm)[0-9]*$")

# --------------------------------------------------------------------------
# Signals that a probe is aimed at this application
# --------------------------------------------------------------------------

# Traversal, including the encodings a scanner uses to slip past a naive check.
# `security_utils.py` already refuses these; this is about *noticing* the
# attempt, not about stopping it.
_TRAVERSAL_RE = re.compile(
    r"(?:\.\./|\.\.\\|%2e%2e|%252e|\.\.%2f|\.\.%5c|/etc/passwd|/proc/self)",
    re.IGNORECASE,
)
_NULL_BYTE_RE = re.compile(r"(?:%00|\x00)")
# Template, script and SQL injection probes. Deliberately narrow: these are
# shapes no legitimate Dikarya URL contains, not a general WAF ruleset.
_INJECTION_RE = re.compile(
    r"(?:\{\{|\$\{|<script|javascript:|onerror=|"
    r"union\s+select|'\s+or\s+'|\bsleep\(|benchmark\(|"
    r"\|\s*sh\b|;\s*curl\b|;\s*wget\b|\$\(.*\))",
    re.IGNORECASE,
)

# Dikarya's own surface. A miss under one of these is someone mapping this app
# rather than sweeping the internet. The caller passes the live set of top-level
# segments from the Flask URL map (see request_diagnostics), so a route added
# later is recognised without editing this list; these are the fallbacks used
# when that set is unavailable.
_APP_SURFACE_PREFIXES = ("/api/", "/job/", "/admin/", "/health", "/auth/")

# Reason codes, stable so they can be grepped and counted.
TARGETED_REASONS = (
    "path_traversal",
    "null_byte",
    "injection_probe",
    "api_surface_probe",
    "admin_probe",
    "job_id_malformed",
)

# Alan 9/12/26 - A plain 401/403 on a real route is deliberately NOT a targeted
# signal. It is overwhelmingly an ordinary authorization outcome -- a logged-out
# user, a missing scope -- and treating it as one both drowned the security
# section and, worse, short-circuited the normal http.request_failed record so
# its developer reason code (scope_required, csrf_token_missing) never reached
# the log at all. Authorization failures stay on the ordinary diagnostics path.

BUCKET_TARGETED = "targeted"
BUCKET_SCANNER = "scanner"


# The verbs Dikarya's own surface uses. Anything else reaching this host is a
# probe by definition: the UI calls endpoints it knows, and no browser invents a
# method. PROPFIND/TRACE/TRACK/CONNECT/SEARCH are the classic ones (WebDAV
# discovery, cross-site tracing, open-proxy tests) and are worth *naming* rather
# than folding into a catch-all, because "which verbs is this IP trying" is the
# question a sweep signature answers.
#
# Alan 9/14/26 - request_diagnostics used to map every method outside its known
# set to the literal string "OTHER" before calling in here, which threw away
# exactly that signal: a PROPFIND sweep and a garbage verb were indistinguishable
# once they arrived. The classifier now receives the real method (bounded and
# stripped to A-Z, but not renamed) and does the mapping itself.
SERVED_METHODS = frozenset({
    "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS",
})

# Reason codes for the scanner bucket, stable so they can be grepped and counted.
SCANNER_REASONS = (
    "known_scanner_path",
    "unsupported_method",
    "unknown_route",
)


def _scanner_reason(path_lower, user_agent, status, method):
    """Why this is internet-wide junk, or None when it is not.

    Software this host does not run and never has, or a verb it does not serve.
    """
    # The verb first: it is the more specific signal, and a PROPFIND sweep
    # answers with 405 on almost every path it tries, so checking the status
    # first would relabel every one of them as a path probe.
    if method and method not in SERVED_METHODS:
        return "unsupported_method"
    # A 405 means the route exists but not for that verb, which the UI never
    # does -- it only calls endpoints it knows. Under /api/ a 405 is instead a
    # real route/frontend mismatch and must stay visible.
    if status == 405 and not path_lower.startswith("/api/"):
        return "known_scanner_path"
    if (
        path_lower.endswith(STATIC_SUFFIXES)
        or path_lower.endswith((".bak", ".sql", ".yml", ".yaml", ".ini"))
        or SCRIPT_EXT_RE.search(path_lower) is not None
        or path_lower in ("/robots.txt", "/.well-known/traffic-advice")
        or path_lower.rstrip("/") in SCANNER_EXACT_PATHS
        or path_lower.startswith("/thumb/")
        or any(marker in path_lower for marker in SCANNER_MARKERS)
        or ("bot" in user_agent.lower() and not path_lower.startswith("/api/"))
    ):
        return "known_scanner_path"
    return None


def normalize_method(value):
    """Bound an untrusted REQUEST_METHOD without changing what it means.

    Kept semantically intact -- PROPFIND stays PROPFIND -- but stripped to
    uppercase letters and length-capped, so an attacker-controlled verb cannot
    carry a newline into a log line or blow up a counter key. An empty or
    entirely non-alphabetic method resolves to "OTHER", which is not a real verb
    and is therefore treated as unserved.
    """
    text = "".join(ch for ch in str(value or "").upper() if "A" <= ch <= "Z")
    return text[:24] or "OTHER"


def _is_app_surface(path_lower, app_segments):
    """Does this path belong to a part of the site the app actually serves?"""
    if path_lower.startswith(_APP_SURFACE_PREFIXES):
        return True
    if not app_segments:
        return False
    first = path_lower.strip("/").split("/", 1)[0]
    return bool(first) and first in app_segments


def classify_request_failure(
    *, path, method="GET", status=404, matched_route=None, user_agent="",
    job_id_valid=None, query="", app_segments=None,
):
    """Return ``(bucket, reason)`` for a failed request.

    ``bucket`` is ``"targeted"``, ``"scanner"`` or ``None`` for an ordinary user
    error. ``reason`` is a stable lowercase code, or ``""`` when the bucket is
    ``None``.

    ``matched_route`` is the Werkzeug rule that matched (``None`` when nothing
    did). ``job_id_valid`` is the result of ``validate_job_id()`` when the route
    carries a job id, and ``None`` when the question does not apply -- passed in
    rather than imported so this module stays dependency-free.

    ``query`` is inspected for injection markers but is never returned and must
    never be logged: it carries user input. ``app_segments`` is the set of
    first path segments the Flask URL map actually serves, so a real page that
    404s is not mistaken for a scanner probe.
    """
    raw = path or "/"
    lower = raw.split("?", 1)[0].lower()
    method = normalize_method(method or "GET")
    # Match against both the raw and the percent-decoded form: a probe arrives
    # encoded (%3Cscript%3E, %2e%2e%2f) precisely to slip past a literal check,
    # and the traversal patterns below cover only some encodings directly.
    combined = raw + ("?" + str(query) if query else "")
    try:
        probe_text = combined + "\n" + unquote(combined)
    except (UnicodeDecodeError, ValueError):
        probe_text = combined

    # --- targeted signals first, whatever the path looks like otherwise ---
    if _TRAVERSAL_RE.search(probe_text):
        return BUCKET_TARGETED, "path_traversal"
    if _NULL_BYTE_RE.search(probe_text):
        return BUCKET_TARGETED, "null_byte"
    if _INJECTION_RE.search(probe_text):
        return BUCKET_TARGETED, "injection_probe"

    # A job route reached with an id that validate_job_id() refuses is someone
    # testing what the id parser accepts. A *valid-shaped* id that 404s is not:
    # ids are short and guessable by design (see the job-id conventions in
    # CLAUDE.md), and ordinary users hit stale links constantly.
    if job_id_valid is False:
        return BUCKET_TARGETED, "job_id_malformed"

    if matched_route is None:
        if lower.startswith("/admin/"):
            return BUCKET_TARGETED, "admin_probe"
        if lower.startswith("/api/") and lower.rstrip("/") not in SCANNER_EXACT_PATHS:
            return BUCKET_TARGETED, "api_surface_probe"

    # --- then internet-wide junk ---
    scanner_reason = _scanner_reason(lower, user_agent or "", status, method)
    if scanner_reason:
        return BUCKET_SCANNER, scanner_reason

    # An unmatched path that is neither a known probe nor part of this app's
    # surface is still junk rather than a user error: nothing in the UI links
    # to it. A path whose first segment the app really serves falls through to
    # None instead, so a genuine broken link -- /whats-new/edit, a renamed
    # page -- stays visible as an ordinary 4xx rather than being filed away.
    if matched_route is None and not _is_app_surface(lower, app_segments):
        return BUCKET_SCANNER, "unknown_route"

    return None, ""
