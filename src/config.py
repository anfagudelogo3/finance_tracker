import os

# Twilio
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_WHATSAPP_NUMBER = os.environ["TWILIO_WHATSAPP_NUMBER"]  # e.g. whatsapp:+14155238886
ALLOWED_PHONE_NUMBERS = set(os.environ["ALLOWED_PHONE_NUMBERS"].split(","))  # e.g. +573001234567,+573009876543

# OpenAI
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

# Database (Neon PostgreSQL)
DATABASE_URL = os.environ["DATABASE_URL"]

# S3
S3_BUCKET_NAME = os.environ["S3_BUCKET_NAME"]

# Webhook
WEBHOOK_URL = os.environ["WEBHOOK_URL"]  # exact URL configured in Twilio sandbox settings
