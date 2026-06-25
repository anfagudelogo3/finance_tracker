import os

# Set placeholder values for all required env vars before any src module is
# imported. config.py reads os.environ at module scope, so these must be in
# place before the first `from <module> import ...` in any test file.
_TEST_ENV = {
    "TWILIO_ACCOUNT_SID": "ACtest00000000000000000000000000",
    "TWILIO_AUTH_TOKEN": "test_auth_token",
    "TWILIO_WHATSAPP_NUMBER": "whatsapp:+14155238886",
    "ALLOWED_PHONE_NUMBERS": "+573001234567",
    "OPENAI_API_KEY": "sk-test",
    "DATABASE_URL": "postgresql://test:test@localhost/testdb",
    "S3_BUCKET_NAME": "test-bucket",
    "WEBHOOK_URL": "https://test.example.com/",
}

for key, value in _TEST_ENV.items():
    os.environ.setdefault(key, value)
