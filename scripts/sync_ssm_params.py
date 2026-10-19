"""Upload the Lambda's settings from a local .env file to SSM Parameter Store.

    uv run python scripts/sync_ssm_params.py --profile <admin-profile>
    uv run python scripts/sync_ssm_params.py --profile <admin-profile> --env-file .env.prod --yes

Each required setting (finance_tracker.core.ssm_config.REQUIRED_SETTINGS) becomes a
SecureString parameter at <path>/<NAME>, overwriting the existing value. Values are
never printed. Run it with credentials allowed to ssm:PutParameter — NOT the deploy-only
user. Re-run it whenever a secret changes; the Lambda picks the new value up on its next
cold start (deploy again to force one).
"""

import argparse
import sys

from dotenv import dotenv_values

from finance_tracker.core.ssm_config import REQUIRED_SETTINGS

DEFAULT_PATH = "/finance-tracker/prod"
DEFAULT_REGION = "us-east-2"

# Values copied from .env.example that must never reach production
_PLACEHOLDER_MARKERS = (
    "your",
    "xxx",
    "user:password@",
    "3001234567",  # the .env.example phone numbers
    "3009876543",
    "sk-not-set",
    "sk-ant-not-set",
    "example.com",
)


def main() -> None:
    # ▶ / → / ✓ in the output: don't crash on a non-UTF-8 Windows console or pipe
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Upload Lambda settings to SSM")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--profile", help="AWS profile allowed to ssm:PutParameter")
    parser.add_argument("--yes", action="store_true", help="Don't ask for confirmation")
    args = parser.parse_args()

    values = dotenv_values(args.env_file)
    missing = [name for name in REQUIRED_SETTINGS if not values.get(name)]
    placeholders = [
        name
        for name in REQUIRED_SETTINGS
        if values.get(name)
        and any(marker in values[name].lower() for marker in _PLACEHOLDER_MARKERS)
    ]
    if missing or placeholders:
        sys.exit(
            f"{args.env_file}: missing {missing or 'none'}; "
            f"placeholder values {placeholders or 'none'}. Fix them first."
        )

    print(f"Will write {len(REQUIRED_SETTINGS)} SecureString parameters to {args.path}")
    print(f"(region {args.region}, overwriting existing values):")
    for name in REQUIRED_SETTINGS:
        print(f"  {args.path}/{name}")
    if not args.yes and input("Continue? [y/N] ").strip().lower() != "y":
        sys.exit("Aborted.")

    import boto3

    ssm = boto3.Session(profile_name=args.profile, region_name=args.region).client(
        "ssm"
    )
    for name in REQUIRED_SETTINGS:
        ssm.put_parameter(
            Name=f"{args.path}/{name}",
            Value=values[name],
            Type="SecureString",
            Overwrite=True,
            Tier="Standard",
        )
        print(f"  ✓ {name}")
    print("Done. Deploy (scripts/deploy.py) to apply on a fresh cold start.")


if __name__ == "__main__":
    main()
