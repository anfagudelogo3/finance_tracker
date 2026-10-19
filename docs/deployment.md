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

## 2. Deploy the code (one command)

[`scripts/deploy.py`](../scripts/deploy.py) tests, builds, uploads and smoke-tests the
function. It runs the same on Windows, macOS and Linux (no `zip` or bash needed):

```bash
uv run python scripts/deploy.py                 # pytest → build → upload → wait → smoke test
uv run python scripts/deploy.py --skip-tests    # skip pytest
uv run python scripts/deploy.py --build-only    # only build lambda.zip, no AWS calls
uv run python scripts/deploy.py --profile my-aws-profile
```

Defaults: function `financeTrackerLambda`, region `us-east-2` (override with
`--function-name` / `--region`). What it does:

1. Runs `uv run pytest tests/ -q` and stops on any failure.
2. Builds `build/` with `uv pip install .` for Lambda's runtime (`x86_64-unknown-linux-gnu`,
   Python 3.12): the `finance_tracker` package plus its **base** dependencies only (never
   the `dev` / `evals` groups). Copies [`lambda/handler.py`](../lambda/handler.py) to the ZIP
   root (it re-exports `finance_tracker.handler.handler`, so the handler setting stays
   `handler.handler`).
3. Zips it into `lambda.zip` (~34 MB; the direct-upload limit is 50 MB).
4. Checks the function's runtime and handler, uploads with `UpdateFunctionCode`, waits until
   the update succeeds, and verifies the deployed `CodeSha256` matches the local zip.
5. Sends a `GET` to the Function URL: the handler answers `405` to anything but `POST`, which
   proves the new code imports and runs without touching Twilio, the database or an LLM.

**Only the code is replaced.** Environment variables (secrets included), memory, timeout and
the handler setting stay as configured on the function. There are no versions or aliases:
to roll back, check out the previous commit and deploy again.

### One-time setup for deploying

1. Install the AWS CLI (used only to store credentials): `winget install Amazon.AWSCLI`
   (macOS: `brew install awscli`).
2. Create an IAM user (or SSO permission set) for deploys with this policy — replace
   `<ACCOUNT_ID>`:

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
           "lambda:GetFunctionUrlConfig"
         ],
         "Resource": "arn:aws:lambda:us-east-2:<ACCOUNT_ID>:function:financeTrackerLambda"
       }
     ]
   }
   ```

3. Create an access key for that user and run `aws configure` (region `us-east-2`), or
   `aws configure --profile finance-tracker` and pass `--profile finance-tracker`.

### First-time function configuration

| Setting | Value |
|---------|-------|
| **Handler** | `handler.handler` |
| **Runtime** | Python 3.12 |
| **Architecture** | x86_64 |
| **Timeout** | 30 seconds (vision + audio calls can be slow) |
| **Memory** | 256–512 MB recommended |
| **Function URL** | Enabled, Auth type **NONE** |
| **Environment variables** | All variables from `.env` — see the full list in [setup.md](setup.md#2-configure-environment-variables) |

> The Function URL is public (`NONE` auth). Security comes from two application-level
> checks: Twilio **signature verification** and the **phone allowlist** (see
> [security](#security) below). The `WEBHOOK_URL` env var must equal the Function URL
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

## 3. Configure the Twilio webhook

In the [Twilio Console](https://console.twilio.com) → **Messaging → Try it out → Send a
WhatsApp message → Sandbox Settings**:

- **When a message comes in:** your Function URL, e.g.
  `https://xxxx.lambda-url.us-east-2.on.aws/`
- **Method:** `HTTP POST`

Set the **same URL** as `WEBHOOK_URL` in the Lambda environment.

## 4. Smoke test

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
| Function fails to start / `KeyError` on a config name | A required environment variable is missing (see [setup.md](setup.md)) |
| Media messages error | Missing S3 permissions on the execution role, or wrong `S3_BUCKET_NAME` |
| Sender gets no reply at all | Number not in `ALLOWED_PHONE_NUMBERS`, or not joined to the Twilio sandbox |
| Timeouts on photos/voice notes | Increase the Lambda timeout/memory; vision and Whisper calls are the slowest steps |
