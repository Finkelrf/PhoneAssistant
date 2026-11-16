"""python -m server.manage pair  -> prints the pairing links (computer and phone)."""
import os
import sys
from pathlib import Path

from .app import ROOT, load_token

data = Path(os.environ.get("REMOTE_DATA", ROOT / "data"))
tok = load_token(data)
url = os.environ.get("REMOTE_URL", "http://127.0.0.1:8796").rstrip("/")
if len(sys.argv) < 2 or sys.argv[1] != "pair":
    sys.exit("usage: python -m server.manage pair")
print(f"Computer: http://127.0.0.1:8796/pair?t={tok}")
print(f"Phone:    {url}/pair?t={tok}")
print("Then open /setup on the computer for the phone's QR code and Spotify.")
