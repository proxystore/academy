# ruff: noqa: D102
from __future__ import annotations

import functools
from collections.abc import Iterable
from collections.abc import Mapping
from typing import Any
from typing import Callable
from typing import TypeVar

from proxystore.proxy import Proxy
from proxystore.store import get_or_create_store
from proxystore.store import register_store
from proxystore.store import Store
from proxystore.store.utils import resolve_async

from academy.behavior import Behavior
from academy.exchange import ExchangeFactory
from academy.exchange import ExchangeTransport
from academy.exchange import MailboxStatus
from academy.identifier import AgentId
from academy.identifier import EntityId
from academy.message import ActionRequest
from academy.message import ActionResponse
from academy.message import Message
from academy.serialize import NoPickleMixin

BehaviorT = TypeVar('BehaviorT', bound=Behavior)


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


class ProxyStoreExchangeFactory(ExchangeFactory):
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
        base: ExchangeFactory,
        store: Store[Any] | None,
        should_proxy: Callable[[Any], bool],
        *,
        resolve_async: bool = False,
    ) -> None:
        self.base = base
        self.store = store
        self.should_proxy = should_proxy
        self.resolve_async = resolve_async

    def _create_transport(
        self,
        mailbox_id: EntityId | None = None,
        *,
        name: str | None = None,
    ) -> ProxyStoreExchangeTransport:
        # If store was none because of pickling,
        # the __setstate__ must be called before bind.
        assert self.store is not None
        transport = self.base._create_transport(mailbox_id, name=name)
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


class ProxyStoreExchangeTransport(ExchangeTransport, NoPickleMixin):
    """ProxyStore exchange client bound to a specific mailbox."""

    def __init__(
        self,
        transport: ExchangeTransport,
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

    def close(self) -> None:
        self.transport.close()

    def discover(
        self,
        behavior: type[Behavior],
        *,
        allow_subclasses: bool = True,
    ) -> tuple[AgentId[Any], ...]:
        return self.transport.discover(
            behavior,
            allow_subclasses=allow_subclasses,
        )

    def factory(self) -> ProxyStoreExchangeFactory:
        return ProxyStoreExchangeFactory(
            self.transport.factory(),
            self.store,
            should_proxy=self.should_proxy,
            resolve_async=self.resolve_async,
        )

    def recv(self, timeout: float | None = None) -> Message:
        message = self.transport.recv(timeout)
        if self.resolve_async and isinstance(message, ActionRequest):
            for arg in (*message.pargs, *message.kargs.values()):
                if type(arg) is Proxy:
                    resolve_async(arg)
        elif (
            self.resolve_async
            and isinstance(message, ActionResponse)
            and type(message.result) is Proxy
        ):
            resolve_async(message.result)
        return message

    def register_agent(
        self,
        behavior: type[BehaviorT],
        *,
        name: str | None = None,
        _agent_id: AgentId[BehaviorT] | None = None,
    ) -> AgentId[BehaviorT]:
        return self.transport.register_agent(
            behavior,
            name=name,
            _agent_id=_agent_id,
        )

    def send(self, uid: EntityId, message: Message) -> None:
        if isinstance(message, ActionRequest):
            message.pargs = _proxy_iterable(
                message.pargs,
                self.store,
                self.should_proxy,
            )
            message.kargs = _proxy_mapping(
                message.kargs,
                self.store,
                self.should_proxy,
            )
        if isinstance(message, ActionResponse) and message.result is not None:
            message.result = _proxy_item(
                message.result,
                self.store,
                self.should_proxy,
            )

        self.transport.send(uid, message)

    def status(self, uid: EntityId) -> MailboxStatus:
        return self.transport.status(uid)

    def terminate(self, uid: EntityId) -> None:
        self.transport.terminate(uid)
