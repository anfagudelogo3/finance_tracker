import logging
import re
from pathlib import Path
from unittest.mock import MagicMock

from finance_tracker.core.ssm_config import REQUIRED_SETTINGS, load_ssm_parameters

_CONFIG_PY = Path(__file__).parents[2] / "src" / "finance_tracker" / "config.py"


def _ssm_client(*pages):
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = [
        {
            "Parameters": [
                {"Name": f"/finance-tracker/prod/{k}", "Value": v} for k, v in page
            ]
        }
        for page in pages
    ]
    return client


def test_no_ssm_path_is_a_noop():
    client = MagicMock()
    environ = {"OPENAI_API_KEY": "local"}

    assert load_ssm_parameters(environ, client) == 0
    client.get_paginator.assert_not_called()
    assert environ == {"OPENAI_API_KEY": "local"}


def test_loads_every_page_into_environ():
    client = _ssm_client(
        [("ANTHROPIC_API_KEY", "sk-ant-x"), ("DATABASE_URL", "postgresql://x")],
        [("WEBHOOK_URL", "https://x")],
    )
    environ = {"CONFIG_SSM_PATH": "/finance-tracker/prod"}

    assert load_ssm_parameters(environ, client) == 3
    assert environ["ANTHROPIC_API_KEY"] == "sk-ant-x"
    assert environ["WEBHOOK_URL"] == "https://x"
    client.get_paginator.return_value.paginate.assert_called_once_with(
        Path="/finance-tracker/prod", WithDecryption=True, Recursive=False
    )


def test_existing_environment_value_wins():
    client = _ssm_client([("OPENAI_API_KEY", "from-ssm")])
    environ = {"CONFIG_SSM_PATH": "/p", "OPENAI_API_KEY": "from-env"}

    assert load_ssm_parameters(environ, client) == 0
    assert environ["OPENAI_API_KEY"] == "from-env"


def test_values_are_never_logged(caplog):
    client = _ssm_client([("TWILIO_AUTH_TOKEN", "super-secret-token")])

    with caplog.at_level(logging.DEBUG):
        load_ssm_parameters({"CONFIG_SSM_PATH": "/p"}, client)

    assert "super-secret-token" not in caplog.text
    assert "missing" in caplog.text  # names of missing settings are fine to log


def test_required_settings_match_what_config_reads():
    """deploy.py removes these from the Lambda env after checking SSM has them — the
    list must cover every os.environ["…"] that config.py requires."""
    required_by_config = set(
        re.findall(r'os\.environ\["(\w+)"\]', _CONFIG_PY.read_text(encoding="utf-8"))
    )
    assert set(REQUIRED_SETTINGS) == required_by_config
