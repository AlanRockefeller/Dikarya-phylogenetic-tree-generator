import os
from app import create_app

# Pick the config from FLASK_ENV (set in .env). Falls back to production by
# default so we never accidentally boot DEBUG=True on the live host.
config_name = os.environ.get('FLASK_ENV', 'production')
app = create_app(config_name)

if __name__ == "__main__":
    app.run(debug=True)
