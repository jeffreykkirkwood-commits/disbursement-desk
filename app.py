# START HERE / FEATURE 01 — Local launch
# Start a local-only demo server. Bind to the loopback address (this computer), disable
# debugger/reloader, and reuse the hosted application rules. Run it deliberately; importing
# this file does not start a server.
"""Local startup for the same Flask application deployed on Render."""
import os
from pathlib import Path
from hosted_app import create_app

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8000'))
    # Bind only to this computer. Render uses Gunicorn instead of Flask's development server.
    application = create_app(os.environ.get('DEMO_DIRECTORY', Path(__file__).with_name('demos')),
                             public_origin=f'http://127.0.0.1:{port}')
    application.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)
