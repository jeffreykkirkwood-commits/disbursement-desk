# START HERE / FEATURE 01 — Local launch
# Run `python app.py` to start the demo on your own computer only (address 127.0.0.1).
# It builds the exact same app that Render hosts (see hosted_app.py); only the startup
# differs. Importing this file from elsewhere does not start a server.
"""Local startup for the same Flask application deployed on Render."""
import os
from pathlib import Path
from hosted_app import create_app

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8000'))
    # Render starts the app with Gunicorn (a production web server) instead of this block.
    application = create_app(os.environ.get('DEMO_DIRECTORY', Path(__file__).with_name('demos')),
                             public_origin=f'http://127.0.0.1:{port}')
    application.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)
