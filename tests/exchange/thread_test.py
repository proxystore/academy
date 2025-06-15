from __future__ import annotations

from academy.exchange.thread import ThreadExchangeFactory


def test_factory_create_client() -> None:
    factory = ThreadExchangeFactory()
    client = factory.create_user_client()
    client.close()
