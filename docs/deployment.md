# Deployment

Finance Tracker runs as a single AWS Lambda function exposed through a Function URL, with
S3 for media/exports and Twilio as the WhatsApp channel. This guide covers packaging,
deploying, and wiring the pieces together.

## Prerequisites

- AWS account with permissions to manage Lambda, S3, and IAM
- AWS credentials for deploying (see [one-time setup](#one-time-setup-for-deploying))
- A Twilio account with the WhatsApp Sandbox enabled
- The database created (see [setup.md](setup.md) and [database.md](database.md))

## 1. Create the S3 bucket

The function reads and writes one bucket (its name comes from `S3_BUCKET_NAME`). It uses
two key prefixes:

| Prefix | Contents | Written by |
|--------|----------|------------|
| `incoming/whatsapp/{audio\|images\|other}/{phone}/{MessageSid}.{ext}` | Archived inbound media | [`media.py`](../src/finance_tracker/storage/media.py) |
| `processed/whatsapp/{phone}/{timestamp}_{min}_{max}.xlsx` | Generated Excel exports | [`excel.py`](../src/finance_tracker/storage/excel.py) |

```bash
aws s3 mb s3://finance-tracker-dev --region us-east-2
```

Keep the bucket **private**. Excel files are shared via short-lived presigned URLs, so no
public access is needed.

## 2. Settings and secrets (SSM Parameter Store)

The function's environment holds **no secrets**. It has a single variable,
`CONFIG_SSM_PATH=/finance-tracker/prod`; every setting `config.py` requires lives under that
path as a **SecureString** parameter named after the variable
(`/finance-tracker/prod/ANTHROPIC_API_KEY`, ...). At cold start `config.py` calls
[`core/ssm_config.load_ssm_parameters`](../src/finance_tracker/core/ssm_config.py), which
loads them with one `GetParametersByPath` call (decrypted with the AWS-managed `aws/ssm` KMS
key). Warm invocations make no SSM calls. Values never appear in logs or in the Lambda
console.

Locally nothing changes: without `CONFIG_SSM_PATH`, settings come from `.env` / the
environment, and an environment value always wins over SSM.

**Upload or change a setting** — from your `.env` (or another file), with credentials that
can write parameters (not the deploy-only user):

```bash
uv run python scripts/sync_ssm_params.py --profile <admin-profile>
uv run python scripts/sync_ssm_params.py --profile <admin-profile> --env-file .env.prod
```

It uploads exactly the nine required settings, refuses missing or placeholder values (copied
from `.env.example`), asks for confirmation, and never prints a value. The Lambda picks up a
new value on its next cold start; deploying forces one.

## 3. Deploy (one command)

[`scripts/deploy.py`](../scripts/deploy.py) tests, builds, uploads, configures and
smoke-tests the function. It runs the same on Windows, macOS and Linux:

```bash
uv run python scripts/deploy.py                 # pytest → build → code → config → smoke test
uv run python scripts/deploy.py --skip-tests    # skip pytest
uv run python scripts/deploy.py --code-only     # upload code, leave the configuration alone
uv run python scripts/deploy.py --build-only    # only build lambda.zip, no AWS calls
uv run python scripts/deploy.py --profile my-aws-profile
```

Defaults: function `financeTrackerLambda`, region `us-east-2` (override with
`--function-name` / `--region`). What it does:

1. Runs `uv run pytest tests/ -q` and stops on any failure.
2. Builds `build/` with `uv pip install .` for Lambda's runtime (`x86_64-unknown-linux-gnu`,
   Python 3.12): the `finance_tracker` package plus its **base** dependencies only (never
   the `dev` / `evals` groups). Copies [`lambda/handler.py`](../lambda/handler.py) to the ZIP
   root (it re-exports `finance_tracker.handler.handler`).
3. Zips it into `lambda.zip` (~34 MB; the direct-upload limit is 50 MB).
4. Checks the function's runtime and handler, and that **every required setting exists in
   SSM** (names only — it never decrypts a value). Stops if any is missing.
5. Uploads the code, waits, and verifies the deployed `CodeSha256` matches the local zip.
6. Applies [`lambda/function_config.json`](../lambda/function_config.json) if it differs from
   the live configuration, printing each change (environment changes by variable **name**
   only).
7. Sends a `GET` to the Function URL: the handler answers `405` to anything but `POST`, which
   proves the new code starts — including loading SSM at cold start — without touching
   Twilio, the database or an LLM. **If this fails after a configuration change, the
   previous configuration is restored** and the script exits with an error.

### Function configuration (`lambda/function_config.json`)

| Setting | Value | Notes |
|---------|-------|-------|
| `Runtime` | `python3.12` | Must match the build target |
| `Handler` | `handler.handler` | `lambda/handler.py` at the zip root |
| `Timeout` | 40 s | Each message's deadline (`core/deadline.py`) is the time left minus `REPLY_RESERVE_SECONDS` (5 s), so a slow message still gets a reply; a settings change or a correction (router + specialist + closing calls) is the slowest Claude path |
| `MemorySize` | 512 MB | At 150 MB every request used all of it (**Max Memory Used** in the CloudWatch `REPORT` lines) and a cold start took 6 s; Lambda CPU also scales with memory |
| `EphemeralStorage` | 512 MB | Lambda's default `/tmp` |
| `Environment` | `CONFIG_SSM_PATH` only | Everything else comes from SSM |

The role, Function URL, architecture (x86_64) and any other setting are left as configured
on the function. To change one of the managed settings, edit the JSON and deploy. There
are no versions or aliases: to roll back code, check out the previous commit and deploy
again.

### One-time setup

1. Install the AWS CLI (used only to store credentials): `winget install Amazon.AWSCLI`
   (macOS: `brew install awscli`).
2. **Deploy user** (IAM user or SSO permission set) — replace `<ACCOUNT_ID>`:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "DeployFinanceTrackerLambda",
         "Effect": "Allow",
         "Action": [
           "lambda:GetFunctionConfiguration",
           "lambda:UpdateFunctionCode",
           "lambda:UpdateFunctionConfiguration",
           "lambda:GetFunctionUrlConfig"
         ],
         "Resource": "arn:aws:lambda:us-east-2:<ACCOUNT_ID>:function:financeTrackerLambda"
       },
       {
         "Sid": "CheckSettingsExistByName",
         "Effect": "Allow",
         "Action": "ssm:GetParametersByPath",
         "Resource": "arn:aws:ssm:us-east-2:<ACCOUNT_ID>:parameter/finance-tracker/prod"
       }
     ]
   }
   ```

   The deploy user has no `kms:Decrypt`, so `GetParametersByPath` only ever shows it
   encrypted values — it can check names, not read secrets.

3. **Lambda execution role** — add, so the function can read its settings:

   ```json
   {
     "Effect": "Allow",
     "Action": "ssm:GetParametersByPath",
     "Resource": "arn:aws:ssm:us-east-2:<ACCOUNT_ID>:parameter/finance-tracker/prod"
   }
   ```

   No KMS statement is needed with the default AWS-managed `aws/ssm` key; with a
   customer-managed key, also allow `kms:Decrypt` on that key.

4. Store deploy credentials: `aws configure` (region `us-east-2`), or
   `aws configure --profile finance-tracker` and pass `--profile finance-tracker`.

### Migrating an existing function to SSM (once)

1. Make sure your `.env` holds the **production** values of all nine settings (the sync
   script refuses placeholders).
2. `uv run python scripts/sync_ssm_params.py --profile <admin-profile>`
3. Add the execution-role statement above, and the new permissions to the deploy user.
4. `uv run python scripts/deploy.py` — it prints
   `Environment: remove [ ... the nine names ... ]`, applies `CONFIG_SSM_PATH`, and the smoke
   test proves the function loads its settings from SSM (or restores the old configuration).
5. Optional: the old values were readable in the Lambda console; rotate the keys if anyone
   else had console access.

### Creating the function the first time

Create a Python 3.12, x86_64 function named `financeTrackerLambda` with a **Function URL**
(auth type **NONE**), then deploy: `scripts/deploy.py` applies everything in
`function_config.json`.

> The Function URL is public (`NONE` auth). Security comes from two application-level
> checks: Twilio **signature verification** and the **phone allowlist** (see
> [security](#security) below). The `WEBHOOK_URL` setting (in SSM) must equal the Function URL
> exactly, or signature checks fail.

### Execution role (IAM)

The Lambda execution role needs, at minimum:

- `AWSLambdaBasicExecutionRole` (CloudWatch Logs)
- S3 access scoped to the bucket:

```json
{
  "Effect": "Allow",
  "Action": ["s3:PutObject", "s3:GetObject"],
  "Resource": "arn:aws:s3:::finance-tracker-dev/*"
}
```

(`GetObject` is required for generating presigned download URLs.)

## 4. Configure the Twilio webhook

In the [Twilio Console](https://console.twilio.com) → **Messaging → Try it out → Send a
WhatsApp message → Sandbox Settings**:

- **When a message comes in:** your Function URL, e.g.
  `https://xxxx.lambda-url.us-east-2.on.aws/`
- **Method:** `HTTP POST`

Set the **same URL** as the `WEBHOOK_URL` setting in SSM (`scripts/sync_ssm_params.py`).

## 5. Smoke test

From a phone joined to the Twilio sandbox and present in `ALLOWED_PHONE_NUMBERS`, send:

```
almuerzo 32000
```

You should receive a confirmation within a few seconds. If not, check **CloudWatch Logs**
— the handler logs each pipeline step (signature validation, message id, parse results,
Twilio SIDs).

## Security

- **Signature verification** — every inbound `POST` is validated against Twilio's HMAC
  ([`webhook.verify_signature`](../src/finance_tracker/channels/whatsapp/webhook.py)); failures return `401`.
- **Phone allowlist** — only senders in `ALLOWED_PHONE_NUMBERS` are processed; others are
  silently dropped.
- **No secrets in code** — all credentials come from environment variables.
- **Private S3 + presigned URLs** — generated files are never public.

For production hardening, consider moving secrets to **AWS SSM Parameter Store** or
**Secrets Manager** and migrating from the Twilio Sandbox to the **WhatsApp Cloud API**.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| Every request returns `401` | `WEBHOOK_URL` doesn't exactly match the URL Twilio calls |
| Function fails to start / `KeyError` on a config name | A setting is missing from SSM (`scripts/sync_ssm_params.py`), or `CONFIG_SSM_PATH` is wrong |
| Function fails to start with `AccessDeniedException` (SSM) | The execution role lacks `ssm:GetParametersByPath` on `/finance-tracker/prod` |
| `Runtime exited` / out-of-memory in `REPORT` lines | `MemorySize` too low — raise it in `lambda/function_config.json` and deploy |
| Media messages error | Missing S3 permissions on the execution role, or wrong `S3_BUCKET_NAME` |
| Sender gets no reply at all | Number not in `ALLOWED_PHONE_NUMBERS`, or not joined to the Twilio sandbox |
| Timeouts on photos/voice notes | Raise `Timeout` / `MemorySize` in `lambda/function_config.json`; vision and Whisper calls are the slowest steps |
