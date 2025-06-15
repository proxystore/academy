from __future__ import annotations

from academy.exchange.redis import RedisExchangeFactory


def test_factory_create_client(mock_redis) -> None:
    factory = RedisExchangeFactory(hostname='localhost', port=0)
    client = factory.create_user_client()
    client.close()
