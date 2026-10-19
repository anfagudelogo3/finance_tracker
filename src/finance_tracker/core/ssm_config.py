"""Load runtime settings from AWS SSM Parameter Store into os.environ.

In production the Lambda's environment holds only CONFIG_SSM_PATH (e.g.
"/finance-tracker/prod"); the secrets and other settings live under that path as
SecureString parameters named after the env var (".../ANTHROPIC_API_KEY"). config.py
calls load_ssm_parameters() once, at import time — i.e. once per Lambda cold start —
before it reads os.environ.

Without CONFIG_SSM_PATH (local runs, tests, evals) this is a no-op and .env / the test
bootstrap work as before. A value already present in the environment always wins over
SSM. Values are never logged.

This module must not import finance_tracker.config (config imports it).
"""

import logging
import os
from collections.abc import MutableMapping

logger = logging.getLogger(__name__)

SSM_PATH_ENV_VAR = "CONFIG_SSM_PATH"

# Every setting config.py requires. scripts/sync_ssm_params.py uploads exactly these,
# and scripts/deploy.py checks they all exist before removing them from the Lambda env.
REQUIRED_SETTINGS = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_WHATSAPP_NUMBER",
    "ALLOWED_PHONE_NUMBERS",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DATABASE_URL",
    "S3_BUCKET_NAME",
    "WEBHOOK_URL",
)


def load_ssm_parameters(
    environ: MutableMapping[str, str] = os.environ, client=None
) -> int:
    """Copy the parameters under $CONFIG_SSM_PATH into `environ` (existing keys win).

    Returns how many values were set. Raises if SSM can't be read — the Lambda should
    fail loudly at cold start rather than run without its configuration.
    """
    path = environ.get(SSM_PATH_ENV_VAR)
    if not path:
        return 0

    if client is None:
        import boto3  # only needed in production; keeps local imports light

        client = boto3.client("ssm")

    loaded = 0
    paginator = client.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=path, WithDecryption=True, Recursive=False):
        for parameter in page["Parameters"]:
            name = parameter["Name"].rsplit("/", 1)[-1]
            if name not in environ:
                environ[name] = parameter["Value"]
                loaded += 1

    missing = [name for name in REQUIRED_SETTINGS if name not in environ]
    logger.info(
        "Loaded %d setting(s) from SSM path %s; missing: %s",
        loaded,
        path,
        missing or "none",
    )
    return loaded
