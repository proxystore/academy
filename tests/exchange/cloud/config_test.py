from __future__ import annotations

import pathlib

from academy.exchange.cloud.config import ExchangeAuthConfig
from academy.exchange.cloud.config import ExchangeServingConfig


def test_auth_config_default() -> None:
    config = ExchangeAuthConfig()
    assert config.method is None


def test_read_from_config_file_empty(tmp_path: pathlib.Path) -> None:
    data = '[serving]'

    filepath = tmp_path / 'relay.toml'
    with open(filepath, 'w') as f:
        f.write(data)

    config = ExchangeServingConfig.from_toml(filepath)
    assert config == ExchangeServingConfig()


def test_read_from_config_file(tmp_path: pathlib.Path) -> None:
    data = """\
host = "localhost"
port = 1234
certfile = "/path/to/cert.pem"
keyfile = "/path/to/privkey.pem"

[auth]
method = "globus"

[auth.kwargs]
client_id = "ABC"
"""

    filepath = tmp_path / 'relay.toml'
    with open(filepath, 'w') as f:
        f.write(data)

    config = ExchangeServingConfig.from_toml(filepath)

    assert config.host == 'localhost'
    assert config.port == 1234  # noqa: PLR2004
    assert config.certfile == '/path/to/cert.pem'
    assert config.keyfile == '/path/to/privkey.pem'

    assert config.auth.method == 'globus'
    assert config.auth.kwargs['client_id'] == 'ABC'
    assert 'client_secret' not in config.auth.kwargs
