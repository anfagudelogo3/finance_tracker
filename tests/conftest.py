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
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "DATABASE_URL": "postgresql://test:test@localhost/testdb",
    "S3_BUCKET_NAME": "test-bucket",
    "WEBHOOK_URL": "https://test.example.com/",
}

for key, value in _TEST_ENV.items():
    os.environ.setdefault(key, value)


import pytest  # noqa: E402  (env vars above must be set before any finance_tracker import)


@pytest.fixture(autouse=True)
def _no_live_llm_calls(monkeypatch):
    """Fail loudly if a test reaches a real Claude/OpenAI client.

    Tests mock clients by patching the module attribute (e.g.
    "finance_tracker.agents.expenses.client"), which replaces the reference and
    bypasses this guard — so only an unmocked call path ever hits it.
    """
    from finance_tracker.core import claude_client
    from finance_tracker.legacy import openai_parser

    def _blocked(*args, **kwargs):
        raise AssertionError("Test made a live LLM API call — mock the client")

    monkeypatch.setattr(claude_client.client.messages, "create", _blocked)
    monkeypatch.setattr(openai_parser.client.chat.completions, "create", _blocked)
    monkeypatch.setattr(openai_parser.client.audio.transcriptions, "create", _blocked)


@pytest.fixture(autouse=True)
def _no_live_database(monkeypatch):
    """Fail loudly if a test opens a real database connection — patch
    finance_tracker.storage.database.get_connection (or the caller's import) instead."""
    import psycopg2

    def _blocked(*args, **kwargs):
        raise AssertionError("Test opened a live database connection — mock it")

    monkeypatch.setattr(psycopg2, "connect", _blocked)


@pytest.fixture(autouse=True)
def _no_deadline():
    """Each test starts without a message deadline (core/deadline.py); tests that need
    one start it with a fake clock."""
    from finance_tracker.core import deadline

    deadline.clear()
    yield
    deadline.clear()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "expects_error_path: the test deliberately makes the handler hit its catch-all "
        "error path (see tests/test_handler.py::_no_hidden_errors)",
    )
