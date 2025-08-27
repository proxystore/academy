from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from typing import Callable
from urllib.parse import parse_qsl

from requests import PreparedRequest


def urlencoded_params_matcher_allow_missing(
    params: Mapping[str, str] | None,
    *,
    allow_blank: bool = False,
) -> Callable[..., Any]:
    """
    Matches URL encoded data

    Args:
        params: Data provided to 'data' arg of request.
    Returns:
        The matcher function.
    """

    def match(request: PreparedRequest) -> tuple[bool, str]:
        reason = ''
        valid = True
        request_body = request.body
        qsl_body = (
            dict(parse_qsl(request_body, keep_blank_values=allow_blank))
            if request_body
            else {}
        )
        params_dict = params or {}
        if request_body is None:  # pragma: no cover
            valid = params is None
        else:
            for key, value in params_dict.items():
                valid = valid and (value == qsl_body.get(key, None))

                if not valid:
                    reason = (
                        f"request.body doesn't match at {key}:"
                        f"{qsl_body.get(key, None)} doesn't match {value}"
                    )
                    break

        return valid, reason

    return match
