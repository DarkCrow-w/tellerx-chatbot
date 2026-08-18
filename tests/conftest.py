from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("QWEN_API_KEY_FILE", "Qwen/Qwen token.txt")

