"""The application object, for tests and tools. Start the server with `python -m cricket_api`, which sets what the API depends on (see serve.py). Running uvicorn on this module by hand
must use one worker, --no-proxy-headers and --no-server-header, and the README does not support it."""
from cricket_api.app import create_app

app = create_app()
