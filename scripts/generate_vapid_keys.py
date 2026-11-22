"""Generate a VAPID key pair for Web Push.

Example:
    python scripts/generate_vapid_keys.py --private-key-out /secure/vapid.pem

Keep the private key out of Git. Put the printed public key in
``VAPID_PUBLIC_KEY`` and point ``VAPID_PRIVATE_KEY_PATH`` at the private file.
"""

from __future__ import annotations

import argparse
import base64
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--private-key-out",
        default="/tmp/taskflow-vapid-private.pem",
        help="Server-only path for the private PEM key.",
    )
    parser.add_argument(
        "--env-out",
        default=None,
        help="Optional 0600 file with VAPID_PUBLIC_KEY and escaped VAPID_PRIVATE_KEY.",
    )
    args = parser.parse_args()

    key = ec.generate_private_key(ec.SECP256R1())
    private_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )

    target = Path(args.private_key_out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(private_bytes)
    target.chmod(0o600)

    public_key = _base64url(public_bytes)
    print(f"VAPID_PUBLIC_KEY={public_key}")
    print(f"VAPID_PRIVATE_KEY_PATH={target}")
    print("VAPID_SUBJECT=mailto:admin@example.com")
    print("Keep the private key server-side and out of Git.")

    if args.env_out:
        env_target = Path(args.env_out)
        env_target.parent.mkdir(parents=True, exist_ok=True)
        escaped_private = private_bytes.decode("ascii").replace("\n", "\\n")
        env_target.write_text(
            f"VAPID_PUBLIC_KEY={public_key}\n"
            f"VAPID_PRIVATE_KEY={escaped_private}\n"
            "VAPID_SUBJECT=mailto:admin@example.com\n",
            encoding="utf-8",
        )
        env_target.chmod(0o600)
        print(f"Wrote {env_target} (0600). Merge it into .env and delete it.")


if __name__ == "__main__":
    main()
