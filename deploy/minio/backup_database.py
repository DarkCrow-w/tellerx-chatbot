"""Offline PostgreSQL backup without exposing credentials in command arguments."""

import argparse
import os
import subprocess
from pathlib import Path

from sqlalchemy.engine import make_url

from app.core.config import Settings

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("destination", type=Path)
args = parser.parse_args()
args.destination.parent.mkdir(parents=True, exist_ok=True)
url = make_url(Settings().database_url)
env = dict(os.environ, PGPASSWORD=url.password or "")
command = [
    "pg_dump",
    "-Fc",
    "-h",
    url.host or "127.0.0.1",
    "-p",
    str(url.port or 5432),
    "-U",
    url.username or "",
    "-d",
    url.database or "",
]
fd = os.open(args.destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    with os.fdopen(fd, "wb") as output:
        subprocess.run(command, env=env, stdout=output, check=True)
except BaseException:
    args.destination.unlink(missing_ok=True)
    raise
print("Database backup saved:", args.destination)
