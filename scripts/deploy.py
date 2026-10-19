"""Build lambda.zip and deploy it to the finance-tracker Lambda — one command.

    uv run python scripts/deploy.py                 # test → build → upload → smoke test
    uv run python scripts/deploy.py --skip-tests    # same, without running pytest first
    uv run python scripts/deploy.py --build-only    # just build lambda.zip, no AWS calls

Only the function CODE is replaced. Environment variables (secrets included), memory,
timeout and the handler setting stay exactly as configured on the function.

AWS credentials come from the normal boto3 chain (`aws configure`, AWS_PROFILE, ...).
The caller needs lambda:GetFunctionConfiguration, lambda:UpdateFunctionCode and
lambda:GetFunctionUrlConfig on the function — see docs/deployment.md.
"""

import argparse
import base64
import hashlib
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

DEFAULT_FUNCTION = "financeTrackerLambda"
DEFAULT_REGION = "us-east-2"
EXPECTED_HANDLER = "handler.handler"

# The Lambda runtime: dependencies are resolved for it, not for this machine
LAMBDA_PYTHON = "3.12"
LAMBDA_PLATFORM = "x86_64-unknown-linux-gnu"

MAX_DIRECT_UPLOAD_BYTES = 50 * 1024 * 1024  # larger zips must go through S3


def main() -> None:
    args = _parse_args()
    started = time.monotonic()

    if not args.skip_tests and not args.build_only:
        _step("Running tests")
        _run(["uv", "run", "pytest", "tests/", "-q"])

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

    client = _lambda_client(args.profile, args.region)
    _step(f"Deploying to {args.function_name} ({args.region})")
    _check_function(client, args.function_name)
    deployed_sha = _upload(client, args.function_name, zip_bytes)
    local_sha = base64.b64encode(hashlib.sha256(zip_bytes).digest()).decode()
    if deployed_sha != local_sha:
        sys.exit(
            f"Code hash mismatch after upload: local {local_sha}, Lambda {deployed_sha}"
        )
    print(f"  CodeSha256: {deployed_sha}")

    _step("Smoke test")
    _smoke_test(client, args.function_name)

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


def _lambda_client(profile: str | None, region: str):
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
    return session.client("lambda")


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


def _smoke_test(client, function_name: str) -> None:
    """GET the Function URL: the handler answers 405 to anything but POST, which proves
    the new code imports and runs (config, tracing, every module) without touching
    Twilio, the database or an LLM."""
    try:
        url = client.get_function_url_config(FunctionName=function_name)["FunctionUrl"]
    except client.exceptions.ResourceNotFoundException:
        print("  no Function URL configured — skipped")
        return
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    if status != 405:
        sys.exit(
            f"Smoke test failed: GET {url} returned {status}, expected 405. Check CloudWatch logs."
        )
    print(f"  GET {url} → 405 as expected")


if __name__ == "__main__":
    main()
