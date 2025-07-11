# ruff: noqa: D102
from __future__ import annotations

import functools
from collections.abc import Iterable
from collections.abc import Mapping
from typing import Any
from typing import Callable
from typing import Generic

from proxystore.proxy import Proxy
from proxystore.store import get_or_create_store
from proxystore.store import register_store
from proxystore.store import Store
from proxystore.store.utils import resolve_async

from academy.agent import Agent
from academy.agent import AgentT
from academy.exchange import ExchangeFactory
from academy.exchange.transport import AgentRegistration
from academy.exchange.transport import AgentRegistrationT
from academy.exchange.transport import ExchangeTransportMixin
from academy.exchange.transport import ExchangeTransportT
from academy.exchange.transport import MailboxStatus
from academy.identifier import AgentId
from academy.identifier import EntityId
from academy.message import ActionRequest
from academy.message import ActionResponse
from academy.message import Message
from academy.serialize import NoPickleMixin


def _proxy_item(
    item: Any,
    store: Store[Any],
    should_proxy: Callable[[Any], bool],
) -> Any:
    if type(item) is not Proxy and should_proxy(item):
        return store.proxy(item)
    return item


def _proxy_iterable(
    items: Iterable[Any],
    store: Store[Any],
    should_proxy: Callable[[Any], bool],
) -> tuple[Any, ...]:
    _apply = functools.partial(
        _proxy_item,
        store=store,
        should_proxy=should_proxy,
    )
    return tuple(map(_apply, items))


def _proxy_mapping(
    mapping: Mapping[Any, Any],
    store: Store[Any],
    should_proxy: Callable[[Any], bool],
) -> dict[Any, Any]:
    _apply = functools.partial(
        _proxy_item,
        store=store,
        should_proxy=should_proxy,
    )
    return {key: _apply(item) for key, item in mapping.items()}


class ProxyStoreExchangeTransport(
    ExchangeTransportMixin,
    NoPickleMixin,
    Generic[ExchangeTransportT],
):
    """ProxyStore exchange client bound to a specific mailbox."""

    def __init__(
        self,
        transport: ExchangeTransportT,
        store: Store[Any],
        should_proxy: Callable[[Any], bool],
        *,
        resolve_async: bool = False,
    ) -> None:
        self.transport = transport
        self.store = store
        self.should_proxy = should_proxy
        self.resolve_async = resolve_async
        register_store(store, exist_ok=True)

    @property
    def mailbox_id(self) -> EntityId:
        return self.transport.mailbox_id

    async def close(self) -> None:
        await self.transport.close()

    async def discover(
        self,
        agent: type[Agent],
        *,
        allow_subclasses: bool = True,
    ) -> tuple[AgentId[Any], ...]:
        return await self.transport.discover(
            agent,
            allow_subclasses=allow_subclasses,
        )

    def factory(self) -> ProxyStoreExchangeFactory[ExchangeTransportT]:
        return ProxyStoreExchangeFactory(
            self.transport.factory(),
            self.store,
            should_proxy=self.should_proxy,
            resolve_async=self.resolve_async,
        )

    async def recv(self, timeout: float | None = None) -> Message[Any]:
        message = await self.transport.recv(timeout)
        body = message.get_body()
        if self.resolve_async and isinstance(body, ActionRequest):
            args = body.get_args()
            kwargs = body.get_kwargs()
            for arg in (*args, *kwargs.values()):
                if type(arg) is Proxy:
                    resolve_async(arg)
        elif (
            self.resolve_async
            and isinstance(body, ActionResponse)
            and type(body.get_result()) is Proxy
        ):
            resolve_async(body.get_result())
        return message

    async def register_agent(
        self,
        agent: type[AgentT],
        *,
        name: str | None = None,
    ) -> AgentRegistration[AgentT]:
        return await self.transport.register_agent(agent, name=name)

    async def send(self, message: Message[Any]) -> None:
        body = message.get_body()
        if isinstance(body, ActionRequest):
            body.pargs = _proxy_iterable(
                body.get_args(),
                self.store,
                self.should_proxy,
            )
            body.kargs = _proxy_mapping(
                body.get_kwargs(),
                self.store,
                self.should_proxy,
            )
        elif isinstance(body, ActionResponse):
            body.result = _proxy_item(
                body.result,
                self.store,
                self.should_proxy,
            )

        await self.transport.send(message)

    async def status(self, uid: EntityId) -> MailboxStatus:
        return await self.transport.status(uid)

    async def terminate(self, uid: EntityId) -> None:
        await self.transport.terminate(uid)


class ProxyStoreExchangeFactory(
    ExchangeFactory[ProxyStoreExchangeTransport[ExchangeTransportT]],
):
    """ProxStore exchange client factory.

    A ProxyStore exchange is used to wrap an underlying exchange so
    large objects may be passed by reference.

    Args:
        base: Base exchange factory.
        store: Store to use for proxying data.
        should_proxy: A callable that returns `True` if an object should be
            proxied. This is applied to every positional and keyword argument
            and result value.
        resolve_async: Resolve proxies asynchronously when received.
    """

    def __init__(
        self,
        base: ExchangeFactory[ExchangeTransportT],
        store: Store[Any] | None,
        should_proxy: Callable[[Any], bool],
        *,
        resolve_async: bool = False,
    ) -> None:
        self.base = base
        self.store = store
        self.should_proxy = should_proxy
        self.resolve_async = resolve_async

    async def _create_transport(
        self,
        mailbox_id: EntityId | None = None,
        *,
        name: str | None = None,
        registration: AgentRegistrationT | None = None,
    ) -> ProxyStoreExchangeTransport[ExchangeTransportT]:
        # If store was none because of pickling,
        # the __setstate__ must be called before bind.
        assert self.store is not None
        transport = await self.base._create_transport(
            mailbox_id,
            name=name,
            registration=registration,
        )
        return ProxyStoreExchangeTransport(
            transport,
            self.store,
            self.should_proxy,
            resolve_async=self.resolve_async,
        )

    def __getstate__(self) -> dict[str, Any]:
        assert self.store is not None

        return {
            'base': self.base,
            'store_config': self.store.config(),
            'resolve_async': self.resolve_async,
            'should_proxy': self.should_proxy,
        }

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.store = get_or_create_store(
            state.pop('store_config'),
            register=True,
        )
        self.__dict__.update(state)
