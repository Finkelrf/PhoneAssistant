import argparse

import uvicorn

from .app import create_app

p = argparse.ArgumentParser(prog="python -m server")
p.add_argument("--host", default="0.0.0.0")
p.add_argument("--port", type=int, default=8796)
p.add_argument("--data", default=None)
a = p.parse_args()
uvicorn.run(create_app(a.data, port=a.port), host=a.host, port=a.port, log_level="warning",
            proxy_headers=False, timeout_graceful_shutdown=2)
