import argparse
import asyncio
import os
from urllib.parse import urlsplit

import uvicorn

from .app import create_app

p = argparse.ArgumentParser(prog="python -m server")
p.add_argument("--host", default="0.0.0.0")
p.add_argument("--port", type=int, default=8796)
p.add_argument("--https-port", type=int, default=int(os.environ.get("REMOTE_HTTPS_PORT") or 0),
               help="also serve HTTPS here (the phone needs it for the microphone); 0 = off")
p.add_argument("--data", default=None)
a = p.parse_args()

secure_url = None
tls = None
if a.https_port:
    from .tls import ensure
    data = a.data or os.environ.get("REMOTE_DATA") or os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    os.makedirs(data, exist_ok=True)
    host = urlsplit(os.environ.get("REMOTE_URL") or "http://127.0.0.1").hostname or "127.0.0.1"
    tls = ensure(data, host)
    secure_url = os.environ.get("REMOTE_SECURE_URL") or f"https://{host}:{a.https_port}"

app = create_app(a.data, port=a.port, secure_url=secure_url)
common = dict(host=a.host, log_level="warning", proxy_headers=False, timeout_graceful_shutdown=2)
servers = [uvicorn.Server(uvicorn.Config(app, port=a.port, **common))]
if tls:
    servers.append(uvicorn.Server(uvicorn.Config(app, port=a.https_port, ssl_certfile=tls["cert"],
                                                 ssl_keyfile=tls["key"], **common)))


async def main():
    await asyncio.gather(*(s.serve() for s in servers))

asyncio.run(main())
