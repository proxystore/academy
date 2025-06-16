from __future__ import annotations

import pickle

import pytest

from academy.exchange.thread import ThreadExchangeFactory


def test_factory_serialize_error(
    thread_exchange_factory: ThreadExchangeFactory,
) -> None:
    with pytest.raises(pickle.PicklingError):
        pickle.dumps(thread_exchange_factory)
