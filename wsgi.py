import os
from app import create_app

# Pick the config from FLASK_ENV (set in .env). Falls back to production by
# default so we never accidentally boot DEBUG=True on the live host.
config_name = os.environ.get('FLASK_ENV', 'production')
app = create_app(config_name)

# Gunicorn imports `wsgi:app` above and never runs this block. It exists only
# for `python wsgi.py` on a workstation, and it used to hard-code debug=True --
# which on any host where that command is how the service starts would expose
# the Werkzeug debugger's interactive console to anyone who can reach a
# traceback. Debug now follows the same config the app was actually built with,
# so production configuration cannot be overridden by the launcher.
if __name__ == "__main__":
    app.run(debug=bool(app.config.get("DEBUG")))
