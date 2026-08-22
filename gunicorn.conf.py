"""Gunicorn settings for the Dikarya web service.

Gunicorn reads ./gunicorn.conf.py from its working directory automatically
(WorkingDirectory=/var/www/dikarya in dikarya-web.service), so this file takes
effect on the next web restart with no change to the root-owned systemd unit.

Command-line flags in ExecStart still win over anything set here. The unit passes
--workers/--threads/--timeout/--bind/--access-logfile/--error-logfile, so those
stay authoritative there; keep this file to settings the unit does not pass.
"""

# Alan 8/14/26 - Add duration/correlation while excluding raw query values.
#
# Gunicorn's default access format carries no timing at all, which made it
# impossible to tell a slow endpoint from a fast one while reading production
# logs. That matters here more than on most deployments: with --workers 4
# --threads 2 there are only 8 request slots, so one handler quietly getting
# slower is what precedes the whole site becoming unresponsive.
#
# Fields, in order:
#   %(h)s  client IP. Real client addresses since ProxyFix was added -- this
#          logged 127.0.0.1 for every request before that, because nginx proxies
#          from localhost.
#   %(t)s  timestamp
#   %(m)s/%(U)s/%(H)s method, query-free path, protocol
#   %(s)s  status
#   %(b)s  response size
#   "-"    referrer deliberately omitted (it may contain query values)
#   %(a)s  user agent
#   %(D)s  request duration in MICROseconds (%(L)s would be seconds)
#
# %(D)s is appended last so existing log parsing that counts fields from the
# left keeps working.
# Referrer is intentionally omitted: it can itself contain OAuth/query values.
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s "-" "%(a)s" %(D)s req=%({x-request-id}o)s'
