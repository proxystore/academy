"""Cloud exchange configuration file parsing."""

from __future__ import annotations

import logging
import pathlib
import sys
from typing import Any
from typing import BinaryIO
from typing import Dict  # noqa: UP035
from typing import Literal
from typing import Optional
from typing import TypeVar
from typing import Union

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from typing import Self
else:  # pragma: <3.11 cover
    from typing_extensions import Self

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    import tomllib
else:  # pragma: <3.11 cover
    import tomli as tomllib  # type: ignore[import-not-found,unused-ignore]


BaseModelT = TypeVar('BaseModelT', bound=BaseModel)


class ExchangeAuthConfig(BaseModel):
    """Exchange authentication configuration.

    Attributes:
        method: Authentication method.
        kwargs: Arbitrary keyword arguments to pass to the authenticator.
            The kwargs are excluded from the [`repr()`][repr] of this
            class because they often contain secrets.
    """

    model_config = ConfigDict(extra='forbid')

    method: Optional[Literal['globus']] = None  # noqa: UP007
    kwargs: Dict[str, Any] = Field(  # noqa: UP006
        default_factory=dict,
        repr=False,
    )


class ExchangeServingConfig(BaseModel):
    """Exchange serving configuration.

    Attributes:
        host: Network interface the server binds to.
        port: Network port the server binds to.
        certfile: Certificate file (PEM format) use to enable TLS.
        keyfile: Private key file. If not specified, the key will be
            taken from the certfile.
        auth: Authentication configuration.
        log_file: Location to write logs.
        log_level: Verbosity of logs.
    """

    host: str = 'localhost'
    port: int = 8700
    certfile: Optional[str] = None  # noqa: UP007
    keyfile: Optional[str] = None  # noqa: UP007
    auth: ExchangeAuthConfig = Field(default_factory=ExchangeAuthConfig)
    log_file: Optional[str] = None  # noqa: UP007
    log_level: Union[int, str] = logging.INFO  # noqa: UP007

    @classmethod
    def from_toml(cls, filepath: str | pathlib.Path) -> Self:
        """Parse an TOML config file.

        Example:
            Minimal config without SSL and without authentication.
            ```toml title="exchange.toml"
            port = 8700
            ```

            ```python
            from academy.exchange.cloud.config import ExchangeServingConfig

            config = ExchangeServingConfig.from_toml('exchange.toml')
            ```

        Example:
            Serve with SSL and Globus Auth.
            ```toml title="relay.toml"
            host = "0.0.0.0"
            port = 8700
            certfile = "/path/to/cert.pem"
            keyfile = "/path/to/privkey.pem"

            [auth]
            method = "globus"

            [auth.kwargs]
            client_id = "..."
            client_secret = "..."
            ```

        Note:
            Omitted values will be set to their defaults (if they are an
            optional value with a default).
            ```toml title="relay.toml"
            [serving]
            certfile = "/path/to/cert.pem"
            ```

            ```python
            from academy.exchange.cloud.config import ExchangeServingConfig

            config = ExchangeServingConfig.from_config('relay.toml')
            assert config.certfile == '/path/to/cert.pem'
            assert config.keyfile is None
            ```
        """
        with open(filepath, 'rb') as f:
            return load(cls, f)


def load(model: type[BaseModelT], fp: BinaryIO) -> BaseModelT:
    """Parse TOML from a binary file to a data class.

    Args:
        model: Config model type to parse TOML using.
        fp: File-like bytes stream to read in.

    Returns:
        Model initialized from TOML file.
    """
    return loads(model, fp.read().decode())


def loads(model: type[BaseModelT], data: str) -> BaseModelT:
    """Parse TOML string to data class.

    Args:
        model: Config model type to parse TOML using.
        data: TOML string to parse.

    Returns:
        Model initialized from TOML file.
    """
    data_dict = tomllib.loads(data)
    return model.model_validate(data_dict, strict=True)
