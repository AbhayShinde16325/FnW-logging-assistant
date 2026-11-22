"""Provision the first administrator and synchronise RBAC rows.

Usage:
    python -m auth.bootstrap --email admin@example.com --name "Admin"
    python -m auth.bootstrap --sync-rbac
"""

from __future__ import annotations

import argparse

from auth.service import bootstrap_admin, sync_rbac


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="Existing or new administrator email")
    parser.add_argument("--name", default="Administrator")
    parser.add_argument("--google-sub", default=None)
    parser.add_argument(
        "--sync-rbac",
        action="store_true",
        help="Only create/synchronise the canonical roles and permissions.",
    )
    args = parser.parse_args()

    if args.sync_rbac:
        sync_rbac()
        print("RBAC roles and permissions synchronised.")

    if args.email:
        employee = bootstrap_admin(
            email=args.email,
            name=args.name,
            google_sub=args.google_sub,
        )
        print(
            f"Administrator ready: {employee.email} "
            f"(employee id {employee.id}, role {employee.role})."
        )

    if not args.email and not args.sync_rbac:
        parser.error("Provide --email or --sync-rbac.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
