# Deployment

Finance Tracker runs as a single AWS Lambda function exposed through a Function URL, with
S3 for media/exports and Twilio as the WhatsApp channel. This guide covers packaging,
deploying, and wiring the pieces together.

## Prerequisites

- AWS account with permissions to manage Lambda, S3, and IAM
- AWS CLI configured (`aws configure`)
- A Twilio account with the WhatsApp Sandbox enabled
- The database created (see [setup.md](setup.md) and [database.md](database.md))

## 1. Create the S3 bucket

The function reads and writes one bucket (its name comes from `S3_BUCKET_NAME`). It uses
two key prefixes:

| Prefix | Contents | Written by |
|--------|----------|------------|
| `incoming/whatsapp/{audio\|images\|other}/{phone}/{MessageSid}.{ext}` | Archived inbound media | [`media.py`](../src/media.py) |
| `processed/whatsapp/{phone}/{timestamp}_{min}_{max}.xlsx` | Generated Excel exports | [`excel.py`](../src/excel.py) |

```bash
aws s3 mb s3://finance-tracker-dev --region us-east-2
```

Keep the bucket **private**. Excel files are shared via short-lived presigned URLs, so no
public access is needed.

## 2. Package the function

[`scripts/deploy.sh`](../scripts/deploy.sh) builds a Lambda-compatible ZIP. It installs
dependencies targeting Lambda's Linux x86_64 runtime, copies the `src/` modules in at the
top level, and zips the result:

```bash
./scripts/deploy.sh
```

This produces `lambda.zip` (gitignored). The script targets
`x86_64-unknown-linux-gnu` / Python 3.12, so it can be run from macOS or Linux.

## 3. Deploy the code

For an existing function:

```bash
aws lambda update-function-code \
  --function-name finance-tracker \
  --zip-file fileb://lambda.zip
```

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

## 4. Configure the Twilio webhook

In the [Twilio Console](https://console.twilio.com) → **Messaging → Try it out → Send a
WhatsApp message → Sandbox Settings**:

- **When a message comes in:** your Function URL, e.g.
  `https://xxxx.lambda-url.us-east-2.on.aws/`
- **Method:** `HTTP POST`

Set the **same URL** as `WEBHOOK_URL` in the Lambda environment.

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
  ([`webhook.verify_signature`](../src/webhook.py)); failures return `401`.
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
