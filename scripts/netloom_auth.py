#!/usr/bin/env python3
"""Get tokens for the netloom API and manage demo users.

Configuration is read from ``terraform output -json`` in infra/app, or from
NETLOOM_* environment variables when Terraform is not available.

    # create (or reset) a user with a permanent password - needs admin AWS creds
    python scripts/netloom_auth.py add-user --email me@example.com --password 'S3curePassw0rd'

    # sign in and print an access token (handles the first-login password change)
    export TOKEN=$(python scripts/netloom_auth.py login --email me@example.com --password '...')

    # machine-to-machine token via OAuth2 client_credentials
    export TOKEN=$(python scripts/netloom_auth.py machine-token)
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import boto3

INFRA = Path(__file__).resolve().parents[1] / "infra" / "app"


def load_config() -> dict:
    cfg = {}
    try:
        raw = subprocess.run(
            ["terraform", f"-chdir={INFRA}", "output", "-json"], capture_output=True, text=True, check=True
        ).stdout
        cfg = {k: v["value"] for k, v in json.loads(raw).items()}
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        pass
    for key in (
        "api_url",
        "region",
        "user_pool_id",
        "user_client_id",
        "automation_client_id",
        "automation_client_secret",
        "token_url",
        "oauth_scope",
    ):
        env = os.environ.get(f"NETLOOM_{key.upper()}")
        if env:
            cfg[key] = env
    return cfg


def need(cfg: dict, *keys: str) -> None:
    missing = [k for k in keys if not cfg.get(k)]
    if missing:
        sys.exit(f"Missing config: {', '.join(missing)}. Run from a deployed checkout or set NETLOOM_<KEY> env vars.")


def cmd_add_user(cfg: dict, args) -> None:
    need(cfg, "user_pool_id", "region")
    idp = boto3.client("cognito-idp", region_name=cfg["region"])
    password = args.password or getpass.getpass("New password: ")
    try:
        idp.admin_create_user(
            UserPoolId=cfg["user_pool_id"],
            Username=args.email,
            UserAttributes=[{"Name": "email", "Value": args.email}, {"Name": "email_verified", "Value": "true"}],
            MessageAction="SUPPRESS",
        )
        print(f"created {args.email}", file=sys.stderr)
    except idp.exceptions.UsernameExistsException:
        print(f"{args.email} exists - resetting password", file=sys.stderr)
    idp.admin_set_user_password(UserPoolId=cfg["user_pool_id"], Username=args.email, Password=password, Permanent=True)


def cmd_login(cfg: dict, args) -> None:
    need(cfg, "user_client_id", "region")
    idp = boto3.client("cognito-idp", region_name=cfg["region"])
    password = args.password or getpass.getpass("Password: ")
    resp = idp.initiate_auth(
        ClientId=cfg["user_client_id"],
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": args.email, "PASSWORD": password},
    )
    if resp.get("ChallengeName") == "NEW_PASSWORD_REQUIRED":
        new_password = args.new_password or getpass.getpass("First login - choose a new password: ")
        resp = idp.respond_to_auth_challenge(
            ClientId=cfg["user_client_id"],
            ChallengeName="NEW_PASSWORD_REQUIRED",
            Session=resp["Session"],
            ChallengeResponses={"USERNAME": args.email, "NEW_PASSWORD": new_password},
        )
    elif resp.get("ChallengeName"):
        sys.exit(f"Unsupported challenge {resp['ChallengeName']} (e.g. MFA) - use the SRP/hosted UI flow instead.")
    print(resp["AuthenticationResult"]["AccessToken"])


def cmd_machine_token(cfg: dict, _args) -> None:
    need(cfg, "automation_client_id", "automation_client_secret", "token_url", "oauth_scope")
    basic = base64.b64encode(f"{cfg['automation_client_id']}:{cfg['automation_client_secret']}".encode()).decode()
    req = urllib.request.Request(
        cfg["token_url"],
        data=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": cfg["oauth_scope"]}).encode(),
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - fixed https endpoint from Terraform
        print(json.load(r)["access_token"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add-user")
    p.add_argument("--email", required=True)
    p.add_argument("--password")
    p = sub.add_parser("login")
    p.add_argument("--email", required=True)
    p.add_argument("--password")
    p.add_argument("--new-password")
    sub.add_parser("machine-token")
    sub.add_parser("config", help="print resolved configuration (secret redacted)")
    args = parser.parse_args()

    cfg = load_config()
    if args.cmd == "config":
        print(json.dumps({k: ("***" if "secret" in k else v) for k, v in cfg.items()}, indent=2))
        return
    {"add-user": cmd_add_user, "login": cmd_login, "machine-token": cmd_machine_token}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
