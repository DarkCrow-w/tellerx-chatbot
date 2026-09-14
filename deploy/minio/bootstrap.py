"""Create private local configuration once, then initialize bucket and application policy."""

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path("/Users/cliff/TellerXData/minio")
COMPOSE = Path(__file__).with_name("compose.yaml").resolve()


def write_once(path, text):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w") as output:
        output.write(text)


def main():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    (ROOT / "data").mkdir(exist_ok=True)
    write_once(
        ROOT / "admin.env",
        "MINIO_ROOT_USER=tellerx-admin\nMINIO_ROOT_PASSWORD=" + secrets.token_hex(32) + "\n",
    )
    write_once(
        ROOT / "application.env",
        "S3_ACCESS_KEY=tellerx-chatbot\nS3_SECRET_KEY="
        + secrets.token_hex(32)
        + "\nS3_ENDPOINT_URL=http://127.0.0.1:9000\nS3_BUCKET=tellerx-knowledge\nS3_REGION=us-east-1\n",
    )
    if "--prepare-only" in sys.argv:
        return
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "up", "-d", "--wait"]
        + ([] if "--no-build" in sys.argv else ["--build"]),
        check=True,
    )
    config = dict(
        line.split("=", 1) for line in (ROOT / "application.env").read_text().splitlines()
    )
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                "Resource": ["arn:aws:s3:::tellerx-knowledge"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                "Resource": ["arn:aws:s3:::tellerx-knowledge/*"],
            },
        ],
    }
    # Generated keys are hex and the username is fixed. Never echo credentials.
    script = (
        """set -eu
mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc mb --ignore-existing local/tellerx-knowledge >/dev/null
mc anonymous set none local/tellerx-knowledge >/dev/null
cat > /tmp/tellerx-policy.json <<'POLICY'
"""
        + json.dumps(policy)
        + """\nPOLICY
mc admin policy create local tellerx-files /tmp/tellerx-policy.json >/dev/null
"""
    )
    import shlex

    user = shlex.quote(config["S3_ACCESS_KEY"])
    password = shlex.quote(config["S3_SECRET_KEY"])
    script += f"if ! mc admin user info local {user} >/dev/null 2>&1; then mc admin user add local {user} {password} >/dev/null; fi\n"
    script += f"mc admin policy attach local tellerx-files --user {user} >/dev/null\n"
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "exec", "-T", "minio", "sh"],
        input=script,
        text=True,
        check=True,
    )
    digest = subprocess.check_output(
        ["docker", "image", "inspect", "tellerx-minio:7aac2a2c-77f82e18", "--format", "{{.Id}}"],
        text=True,
    ).strip()
    (ROOT / "image-digest.txt").write_text(digest + "\n")
    print("MinIO initialized; credentials remain in", ROOT)


if __name__ == "__main__":
    main()
