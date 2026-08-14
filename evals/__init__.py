"""Bootstrap for the evals package.

Sets up sys.path and environment variables before any src module is imported,
so config.py (which reads env vars at module scope) can load cleanly with only
OPENAI_API_KEY coming from the real .env file.
"""
import os
import sys
from pathlib import Path

# Put src/ on the path so evals can import parser, tracing, config, etc.
_src = Path(__file__).parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

# Load .env so OPENAI_API_KEY (and any real overrides) are picked up first.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Dummy values for env vars required by config.py that are not needed
# during eval runs. setdefault means a real .env value always wins.
_DUMMY_ENV = {
    "TWILIO_ACCOUNT_SID": "ACtest00000000000000000000000000",
    "TWILIO_AUTH_TOKEN": "test_auth_token",
    "TWILIO_WHATSAPP_NUMBER": "whatsapp:+14155238886",
    "ALLOWED_PHONE_NUMBERS": "+573001234567",
    "OPENAI_API_KEY": "sk-not-set",
    "ANTHROPIC_API_KEY": "sk-ant-not-set",
    "DATABASE_URL": "postgresql://test:test@localhost/testdb",
    "S3_BUCKET_NAME": "test-bucket",
    "WEBHOOK_URL": "https://test.example.com/",
}

for _key, _val in _DUMMY_ENV.items():
    os.environ.setdefault(_key, _val)
