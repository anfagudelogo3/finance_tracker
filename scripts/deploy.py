"""Build lambda.zip and deploy it to the finance-tracker Lambda — one command.

    uv run python scripts/deploy.py                 # test → build → upload code → apply config → smoke test
    uv run python scripts/deploy.py --skip-tests    # same, without running pytest first
    uv run python scripts/deploy.py --code-only     # upload code, leave the function configuration alone
    uv run python scripts/deploy.py --build-only    # just build lambda.zip, no AWS calls

Function configuration (runtime, handler, timeout, memory, ephemeral storage and the
environment) comes from lambda/function_config.json and is applied after the code. The
environment holds no secrets: only CONFIG_SSM_PATH, from which the function loads its
settings at cold start (see finance_tracker/core/ssm_config.py). Before applying the
environment, the script checks that every required parameter exists under that path —
by name only, it never decrypts a value. If the smoke test fails after a configuration
change, the previous configuration is restored.

AWS credentials come from the normal boto3 chain (`aws configure`, AWS_PROFILE, ...).
Required permissions are listed in docs/deployment.md.
"""

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"
ZIP_PATH = ROOT / "lambda.zip"
ENTRY_POINT = ROOT / "lambda" / "handler.py"
FUNCTION_CONFIG_PATH = ROOT / "lambda" / "function_config.json"

DEFAULT_FUNCTION = "financeTrackerLambda"
DEFAULT_REGION = "us-east-2"
EXPECTED_HANDLER = "handler.handler"

# The Lambda runtime: dependencies are resolved for it, not for this machine
LAMBDA_PYTHON = "3.12"
LAMBDA_PLATFORM = "x86_64-unknown-linux-gnu"

MAX_DIRECT_UPLOAD_BYTES = 50 * 1024 * 1024  # larger zips must go through S3

# Configuration fields this script manages. Everything else (role, VPC, layers, the
# Function URL) is left as configured on the function.
MANAGED_FIELDS = (
    "Runtime",
    "Handler",
    "Timeout",
    "MemorySize",
    "EphemeralStorage",
    "Environment",
)


def main() -> None:
    args = _parse_args()
    # ▶ / → / ✓ in the output: don't crash on a non-UTF-8 Windows console or pipe
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    started = time.monotonic()

    if not args.skip_tests and not args.build_only:
        _step("Running tests")
        _run(["uv", "run", "pytest", "tests/", "-q"])

    desired = None if args.code_only else _load_function_config()

    _step("Building package for the Lambda runtime")
    _build()
    zip_bytes = _zip()
    size_mb = len(zip_bytes) / 1024 / 1024
    print(f"  lambda.zip: {size_mb:.1f} MB")
    if len(zip_bytes) > MAX_DIRECT_UPLOAD_BYTES:
        sys.exit(
            f"lambda.zip is {size_mb:.1f} MB, over the 50 MB direct-upload limit — "
            "upload through S3 instead (not supported by this script yet)."
        )
    if args.build_only:
        print(f"\nBuilt {ZIP_PATH} (not deployed).")
        return

    session = _aws_session(args.profile, args.region)
    client = session.client("lambda")
    _step(f"Deploying to {args.function_name} ({args.region})")
    _check_function(client, args.function_name)
    if desired:
        _check_ssm_parameters(session.client("ssm"), desired)

    deployed_sha = _upload(client, args.function_name, zip_bytes)
    local_sha = base64.b64encode(hashlib.sha256(zip_bytes).digest()).decode()
    if deployed_sha != local_sha:
        sys.exit(
            f"Code hash mismatch after upload: local {local_sha}, Lambda {deployed_sha}"
        )
    print(f"  CodeSha256: {deployed_sha}")

    previous = None
    if desired:
        _step("Function configuration")
        previous = _apply_config(client, args.function_name, desired)

    _step("Smoke test")
    if not _smoke_test(client, args.function_name):
        if previous:
            print("  restoring the previous configuration...")
            _update_config(client, args.function_name, previous)
            sys.exit(
                "Smoke test failed — previous configuration restored. "
                "Check CloudWatch logs."
            )
        sys.exit("Smoke test failed. Check CloudWatch logs.")

    print(f"\nDeployed in {time.monotonic() - started:.0f} s.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and deploy the finance-tracker Lambda"
    )
    parser.add_argument("--function-name", default=DEFAULT_FUNCTION)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument(
        "--profile", help="AWS profile name (default: the boto3 credential chain)"
    )
    parser.add_argument(
        "--skip-tests", action="store_true", help="Don't run pytest first"
    )
    parser.add_argument(
        "--build-only", action="store_true", help="Build lambda.zip, don't deploy"
    )
    parser.add_argument(
        "--code-only",
        action="store_true",
        help="Upload code only; don't apply lambda/function_config.json",
    )
    return parser.parse_args()


def _step(title: str) -> None:
    print(f"\n▶ {title}")


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        sys.exit(f"Command failed ({result.returncode}): {' '.join(cmd)}")


def _build() -> None:
    """Install the finance_tracker package and its base dependencies (never the dev /
    evals groups) into build/, plus the zip-root entry point lambda/handler.py."""
    shutil.rmtree(BUILD_DIR, ignore_errors=True)
    _run(
        [
            "uv",
            "pip",
            "install",
            ".",
            "--target",
            str(BUILD_DIR),
            "--python-platform",
            LAMBDA_PLATFORM,
            "--python",
            LAMBDA_PYTHON,
            "--quiet",
        ]
    )
    shutil.copy2(ENTRY_POINT, BUILD_DIR / "handler.py")


def _zip() -> bytes:
    """Zip build/ into lambda.zip. Files get explicit 0644 permissions so a zip made on
    Windows is still readable by the Lambda runtime; bytecode caches are skipped."""
    ZIP_PATH.unlink(missing_ok=True)
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(BUILD_DIR.rglob("*")):
            if (
                not path.is_file()
                or "__pycache__" in path.parts
                or path.suffix == ".pyc"
            ):
                continue
            info = zipfile.ZipInfo.from_file(
                path, path.relative_to(BUILD_DIR).as_posix()
            )
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
    return ZIP_PATH.read_bytes()


def _aws_session(profile: str | None, region: str):
    import boto3  # imported here so --build-only works without AWS set up
    from botocore.exceptions import NoCredentialsError, ProfileNotFound

    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        if session.get_credentials() is None:
            raise NoCredentialsError()
    except (NoCredentialsError, ProfileNotFound) as exc:
        sys.exit(
            f"No usable AWS credentials ({exc}). Run `aws configure` (or pass --profile)."
        )
    return session


def _load_function_config() -> dict:
    config = json.loads(FUNCTION_CONFIG_PATH.read_text(encoding="utf-8"))
    name = FUNCTION_CONFIG_PATH.name
    unknown = set(config) - set(MANAGED_FIELDS)
    if unknown:
        sys.exit(f"{name}: unsupported field(s) {sorted(unknown)}")
    if config.get("Runtime") != f"python{LAMBDA_PYTHON}":
        sys.exit(f"{name}: Runtime must be python{LAMBDA_PYTHON} (the build target).")
    if config.get("Handler") != EXPECTED_HANDLER:
        sys.exit(f"{name}: Handler must be {EXPECTED_HANDLER}.")
    return config


def _check_function(client, function_name: str) -> None:
    config = client.get_function_configuration(FunctionName=function_name)
    print(
        f"  runtime={config['Runtime']} handler={config['Handler']} "
        f"memory={config['MemorySize']} MB timeout={config['Timeout']} s"
    )
    if config["Handler"] != EXPECTED_HANDLER:
        sys.exit(
            f"Handler is {config['Handler']!r}, expected {EXPECTED_HANDLER!r} "
            "(lambda/handler.py is copied to the zip root as handler.py)."
        )
    if not config["Runtime"].startswith(f"python{LAMBDA_PYTHON}"):
        sys.exit(
            f"Runtime is {config['Runtime']}, but the package is built for Python {LAMBDA_PYTHON}."
        )


def _check_ssm_parameters(ssm, desired: dict) -> None:
    """Refuse to deploy an environment that relies on SSM unless every required
    setting exists there. Names only: WithDecryption=False never reveals a value."""
    from finance_tracker.core.ssm_config import REQUIRED_SETTINGS, SSM_PATH_ENV_VAR

    path = desired.get("Environment", {}).get("Variables", {}).get(SSM_PATH_ENV_VAR)
    if not path:
        return
    names = set()
    paginator = ssm.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=path, WithDecryption=False, Recursive=False):
        names.update(p["Name"].rsplit("/", 1)[-1] for p in page["Parameters"])
    missing = [name for name in REQUIRED_SETTINGS if name not in names]
    if missing:
        sys.exit(
            f"SSM path {path} is missing {missing}. Upload them first with:\n"
            "  uv run python scripts/sync_ssm_params.py --profile <admin-profile>"
        )
    print(f"  SSM {path}: all {len(REQUIRED_SETTINGS)} settings present")


def _upload(client, function_name: str, zip_bytes: bytes) -> str:
    response = client.update_function_code(
        FunctionName=function_name, ZipFile=zip_bytes
    )
    print("  uploaded, waiting for the update to finish...")
    client.get_waiter("function_updated").wait(FunctionName=function_name)
    config = client.get_function_configuration(FunctionName=function_name)
    if config.get("LastUpdateStatus") != "Successful":
        sys.exit(
            f"Update did not succeed: {config.get('LastUpdateStatus')} {config.get('LastUpdateStatusReason', '')}"
        )
    return config["CodeSha256"] or response["CodeSha256"]


def _apply_config(client, function_name: str, desired: dict) -> dict | None:
    """Apply `desired` if it differs from the live configuration. Returns the previous
    configuration (to restore if the smoke test fails), or None if nothing changed.
    Environment differences are printed by variable NAME only — never values."""
    current = client.get_function_configuration(FunctionName=function_name)
    previous = {
        "Runtime": current["Runtime"],
        "Handler": current["Handler"],
        "Timeout": current["Timeout"],
        "MemorySize": current["MemorySize"],
        "EphemeralStorage": {
            "Size": current.get("EphemeralStorage", {}).get("Size", 512)
        },
        "Environment": {
            "Variables": current.get("Environment", {}).get("Variables", {})
        },
    }

    changes = []
    for field in ("Runtime", "Handler", "Timeout", "MemorySize"):
        if field in desired and desired[field] != previous[field]:
            changes.append(f"{field}: {previous[field]} → {desired[field]}")
    if (
        "EphemeralStorage" in desired
        and desired["EphemeralStorage"] != previous["EphemeralStorage"]
    ):
        changes.append(
            f"EphemeralStorage: {previous['EphemeralStorage']['Size']} → "
            f"{desired['EphemeralStorage']['Size']} MB"
        )
    if "Environment" in desired:
        old_env = previous["Environment"]["Variables"]
        new_env = desired["Environment"]["Variables"]
        removed = sorted(set(old_env) - set(new_env))
        added = sorted(set(new_env) - set(old_env))
        changed = sorted(
            k for k in set(old_env) & set(new_env) if old_env[k] != new_env[k]
        )
        if removed:
            changes.append(f"Environment: remove {removed}")
        if added:
            changes.append(f"Environment: add {added}")
        if changed:
            changes.append(f"Environment: change {changed}")

    if not changes:
        print("  already up to date")
        return None
    for change in changes:
        print(f"  {change}")
    _update_config(client, function_name, desired)
    return previous


def _update_config(client, function_name: str, config: dict) -> None:
    client.update_function_configuration(FunctionName=function_name, **config)
    client.get_waiter("function_updated").wait(FunctionName=function_name)
    status = client.get_function_configuration(FunctionName=function_name)
    if status.get("LastUpdateStatus") != "Successful":
        sys.exit(
            f"Configuration update did not succeed: {status.get('LastUpdateStatus')} "
            f"{status.get('LastUpdateStatusReason', '')}"
        )
    print("  configuration applied")


def _smoke_test(client, function_name: str) -> bool:
    """GET the Function URL: the handler answers 405 to anything but POST, which proves
    the new code starts — including loading its settings from SSM at cold start — and
    runs, without touching Twilio, the database or an LLM."""
    try:
        url = client.get_function_url_config(FunctionName=function_name)["FunctionUrl"]
    except client.exceptions.ResourceNotFoundException:
        print("  no Function URL configured — skipped")
        return True
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    if status != 405:
        print(f"  GET {url} returned {status}, expected 405")
        return False
    print(f"  GET {url} → 405 as expected")
    return True


if __name__ == "__main__":
    main()
