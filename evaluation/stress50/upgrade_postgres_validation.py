"""Run the existing cleanup integration test in an empty, disposable database."""
import os
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from evaluation.stress50 import run

run.configure(SimpleNamespace(model_env=Path('/Users/cliff/Documents/TellerxChatBot/.env'),connection_doc=Path('/Users/cliff/.codex/worktrees/POSTGRESQL_SHARED.md'),key_file=Path('/Users/cliff/Documents/TellerxChatBot/Qwen/Qwen token.txt'),embedding_model='qwen3.7-text-embedding-flash',chat_model='qwen3.7-flash'))
name='tellerx_upgrade_check_'+uuid.uuid4().hex[:12]
url=make_url(os.environ['DATABASE_URL'])
admin=create_engine(url.set(database='postgres'),isolation_level='AUTOCOMMIT')
with admin.connect() as conn: conn.execute(text(f'CREATE DATABASE "{name}"'))
try:
    env=dict(os.environ)
    env['DATABASE_URL']=url.set(database=name).render_as_string(hide_password=False)
    env['TEST_DATABASE_URL']=env['DATABASE_URL']
    env['EMBEDDING_DIMENSIONS']='2560'
    env['EMBEDDING_MODEL']='qwen3-embedding'
    subprocess.run(['.venv/bin/python','-m','alembic','upgrade','head'],env=env,check=True,stdout=subprocess.DEVNULL)
    subprocess.run(['.venv/bin/python','-m','pytest','-q','tests/test_project_cleanup_postgresql.py'],env=env,check=True)
finally:
    with admin.connect() as conn: conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()
