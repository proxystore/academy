from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest import mock

import pytest

from academy.agent import action
from academy.agent import Agent
from academy.agent import loop
from academy.context import ActionContext
from academy.exception import ActionCancelledError
from academy.exchange import UserExchangeClient
from academy.exchange.local import LocalExchangeTransport
from academy.exchange.transport import MailboxStatus
from academy.handle import Handle
from academy.handle import HandleDict
from academy.handle import HandleList
from academy.handle import ProxyHandle
from academy.handle import RemoteHandle
from academy.handle import UnboundRemoteHandle
from academy.identifier import AgentId
from academy.identifier import EntityId
from academy.message import ActionRequest
from academy.message import ActionResponse
from academy.message import ErrorResponse
from academy.message import Message
from academy.message import PingRequest
from academy.message import ShutdownRequest
from academy.message import SuccessResponse
from academy.runtime import _bind_agent_handles
from academy.runtime import Runtime
from academy.runtime import RuntimeConfig
from testing.agents import CounterAgent
from testing.agents import EmptyAgent
from testing.agents import ErrorAgent
from testing.constant import TEST_SLEEP_INTERVAL
from testing.constant import TEST_WAIT_TIMEOUT


class SignalingAgent(Agent):
    def __init__(self) -> None:
        super().__init__()
        self.startup_event = asyncio.Event()
        self.shutdown_event = asyncio.Event()

    async def agent_on_startup(self) -> None:
        self.startup_event.set()

    async def agent_on_shutdown(self) -> None:
        self.shutdown_event.set()

    @loop
    async def shutdown_immediately(self, shutdown: asyncio.Event) -> None:
        shutdown.set()


@pytest.mark.asyncio
async def test_runtime_context_manager(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(SignalingAgent)
    async with Runtime(
        EmptyAgent(),
        exchange_factory=exchange_client.factory(),
        registration=registration,
    ) as runtime:
        assert isinstance(repr(runtime), str)
        assert isinstance(str(runtime), str)


@pytest.mark.asyncio
async def test_runtime_run_until_complete(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(SignalingAgent)
    runtime = Runtime(
        SignalingAgent(),
        exchange_factory=exchange_client.factory(),
        registration=registration,
    )
    await runtime.run_until_complete()

    with pytest.raises(RuntimeError, match='Agent has already been shutdown.'):
        await runtime.run_until_complete()

    assert runtime.agent.startup_event.is_set()
    assert runtime.agent.shutdown_event.is_set()


@pytest.mark.asyncio
async def test_runtime_run_until_complete_as_task(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(SignalingAgent)
    runtime = Runtime(
        SignalingAgent(),
        exchange_factory=exchange_client.factory(),
        registration=registration,
    )

    task = asyncio.create_task(
        runtime.run_until_complete(),
        name='test-runtime-run-until-complete-at-task',
    )
    await task

    assert runtime.agent.startup_event.is_set()
    assert runtime.agent.shutdown_event.is_set()


@pytest.mark.asyncio
async def test_runtime_shutdown_without_terminate(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(SignalingAgent)
    runtime = Runtime(
        SignalingAgent(),
        exchange_factory=exchange_client.factory(),
        registration=registration,
        config=RuntimeConfig(terminate_on_success=False),
    )
    await runtime.run_until_complete()
    assert runtime._shutdown_options.expected_shutdown
    assert (
        await exchange_client.status(runtime.agent_id) == MailboxStatus.ACTIVE
    )


@pytest.mark.asyncio
async def test_runtime_shutdown_terminate_override(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(EmptyAgent)

    async with Runtime(
        EmptyAgent(),
        exchange_factory=exchange_client.factory(),
        registration=registration,
        config=RuntimeConfig(
            terminate_on_success=False,
            terminate_on_error=False,
        ),
    ) as runtime:
        runtime.signal_shutdown(expected=True, terminate=True)
        await runtime.wait_shutdown(timeout=TEST_WAIT_TIMEOUT)

    assert (
        await exchange_client.status(runtime.agent_id)
        == MailboxStatus.TERMINATED
    )


@pytest.mark.asyncio
async def test_runtime_startup_failure(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(SignalingAgent)
    runtime = Runtime(
        SignalingAgent(),
        exchange_factory=exchange_client.factory(),
        registration=registration,
    )

    with mock.patch.object(runtime, '_start', side_effect=Exception('Oops!')):
        with pytest.raises(Exception, match='Oops!'):
            await runtime.run_until_complete()

    assert not runtime.agent.startup_event.is_set()
    assert not runtime.agent.shutdown_event.is_set()


@pytest.mark.asyncio
async def test_runtime_wait_shutdown_timeout(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(SignalingAgent)
    async with Runtime(
        EmptyAgent(),
        exchange_factory=exchange_client.factory(),
        registration=registration,
    ) as runtime:
        with pytest.raises(TimeoutError):
            await runtime.wait_shutdown(timeout=TEST_SLEEP_INTERVAL)


class LoopFailureAgent(Agent):
    @loop
    async def bad1(self, shutdown: asyncio.Event) -> None:
        raise RuntimeError('Loop failure 1.')

    @loop
    async def bad2(self, shutdown: asyncio.Event) -> None:
        raise RuntimeError('Loop failure 2.')


@pytest.mark.parametrize('raise_errors', (True, False))
@pytest.mark.asyncio
async def test_runtime_loop_error_causes_shutdown(
    raise_errors: bool,
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(LoopFailureAgent)
    runtime = Runtime(
        LoopFailureAgent(),
        exchange_factory=exchange_client.factory(),
        registration=registration,
        config=RuntimeConfig(raise_loop_errors_on_shutdown=raise_errors),
    )

    if not raise_errors:
        await asyncio.wait_for(
            runtime.run_until_complete(),
            timeout=TEST_WAIT_TIMEOUT,
        )
    elif sys.version_info >= (3, 11):  # pragma: >=3.11 cover
        # In Python 3.11 and later, all exceptions are raised in a group.
        with pytest.raises(ExceptionGroup) as exc_info:  # noqa: F821
            await asyncio.wait_for(
                runtime.run_until_complete(),
                timeout=TEST_WAIT_TIMEOUT,
            )
        assert len(exc_info.value.exceptions) == 2  # noqa: PLR2004
    else:  # pragma: <3.11 cover
        # In Python 3.10 and older, only the first error will be raised.
        with pytest.raises(RuntimeError, match='Loop failure'):
            await asyncio.wait_for(
                runtime.run_until_complete(),
                timeout=TEST_WAIT_TIMEOUT,
            )


@pytest.mark.asyncio
async def test_runtime_loop_error_without_shutdown(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(LoopFailureAgent)
    runtime = Runtime(
        LoopFailureAgent(),
        exchange_factory=exchange_client.factory(),
        registration=registration,
        config=RuntimeConfig(shutdown_on_loop_error=False),
    )

    task = asyncio.create_task(
        runtime.run_until_complete(),
        name='test-runtime-loop-error-without-shutdown',
    )
    await runtime._started_event.wait()

    # Should timeout because agent did not shutdown after loop errors
    done, pending = await asyncio.wait({task}, timeout=TEST_SLEEP_INTERVAL)
    assert len(done) == 0
    runtime.signal_shutdown()

    # Loop errors raised on shutdown
    if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
        # In Python 3.11 and later, all exceptions are raised in a group.
        with pytest.raises(ExceptionGroup) as exc_info:  # noqa: F821
            await task
        assert len(exc_info.value.exceptions) == 2  # noqa: PLR2004
    else:  # pragma: <3.11 cover
        # In Python 3.10 and older, only the first error will be raised.
        with pytest.raises(RuntimeError, match='Loop failure'):
            await task


@pytest.mark.asyncio
async def test_runtime_shutdown_message(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(EmptyAgent)

    async with Runtime(
        EmptyAgent(),
        exchange_factory=exchange_client.factory(),
        registration=registration,
    ) as runtime:
        shutdown = Message.create(
            src=exchange_client.client_id,
            dest=runtime.agent_id,
            body=ShutdownRequest(),
        )
        await exchange_client.send(shutdown)
        await runtime.wait_shutdown(timeout=TEST_WAIT_TIMEOUT)


@pytest.mark.asyncio
async def test_runtime_ping_message(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(EmptyAgent)
    # Cancel listener so test can intercept agent responses
    await exchange_client._stop_listener_task()

    async with Runtime(
        EmptyAgent(),
        exchange_factory=exchange_client.factory(),
        registration=registration,
    ) as runtime:
        ping = Message.create(
            src=exchange_client.client_id,
            dest=runtime.agent_id,
            body=PingRequest(),
        )
        await exchange_client.send(ping)
        message = await exchange_client._transport.recv()
        assert isinstance(message.get_body(), SuccessResponse)


@pytest.mark.asyncio
async def test_runtime_action_message(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(CounterAgent)
    # Cancel listener so test can intercept agent responses
    await exchange_client._stop_listener_task()

    async with Runtime(
        CounterAgent(),
        exchange_factory=exchange_client.factory(),
        registration=registration,
    ) as runtime:
        value = 42
        request = Message.create(
            src=exchange_client.client_id,
            dest=runtime.agent_id,
            body=ActionRequest(action='add', pargs=(value,)),
        )
        await exchange_client.send(request)
        message = await exchange_client._transport.recv()
        body = message.get_body()
        assert isinstance(body, ActionResponse)
        assert body.get_result() is None

        request = Message.create(
            src=exchange_client.client_id,
            dest=runtime.agent_id,
            body=ActionRequest(action='count'),
        )
        await exchange_client.send(request)
        message = await exchange_client._transport.recv()
        body = message.get_body()
        assert isinstance(body, ActionResponse)
        assert body.get_result() == value


@pytest.mark.parametrize('cancel', (True, False))
@pytest.mark.asyncio
async def test_runtime_cancel_action_requests_on_shutdown(
    cancel: bool,
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    class NoReturnAgent(Agent):
        @action
        async def sleep(self) -> None:
            await asyncio.sleep(1000 if cancel else TEST_SLEEP_INTERVAL)

    registration = await exchange_client.register_agent(ErrorAgent)
    # Cancel listener so test can intercept agent responses
    await exchange_client._stop_listener_task()

    runtime = Runtime(
        NoReturnAgent(),
        exchange_factory=exchange_client.factory(),
        registration=registration,
        config=RuntimeConfig(cancel_actions_on_shutdown=cancel),
    )
    task = asyncio.create_task(
        runtime.run_until_complete(),
        name='test-runtime-cancel-action-requests-on-shutdown',
    )
    await runtime._started_event.wait()

    request = Message.create(
        src=exchange_client.client_id,
        dest=runtime.agent_id,
        body=ActionRequest(action='sleep'),
    )
    await exchange_client.send(request)

    shutdown = Message.create(
        src=exchange_client.client_id,
        dest=runtime.agent_id,
        body=ShutdownRequest(),
    )
    await exchange_client.send(shutdown)

    message = await exchange_client._transport.recv()
    body = message.get_body()
    if cancel:
        assert isinstance(body, ErrorResponse)
        assert isinstance(body.get_exception(), ActionCancelledError)
    else:
        assert isinstance(body, ActionResponse)
        assert body.get_result() is None

    await asyncio.wait_for(task, timeout=TEST_WAIT_TIMEOUT)


@pytest.mark.asyncio
async def test_runtime_action_message_error(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(ErrorAgent)
    # Cancel listener so test can intercept agent responses
    await exchange_client._stop_listener_task()

    async with Runtime(
        ErrorAgent(),
        exchange_factory=exchange_client.factory(),
        registration=registration,
    ) as runtime:
        request = Message.create(
            src=exchange_client.client_id,
            dest=runtime.agent_id,
            body=ActionRequest(action='fails'),
        )
        await exchange_client.send(request)
        message = await exchange_client._transport.recv()
        body = message.get_body()
        assert isinstance(body, ErrorResponse)
        exception = body.get_exception()
        assert isinstance(exception, RuntimeError)
        assert 'This action always fails.' in str(exception)


@pytest.mark.asyncio
async def test_runtime_action_message_unknown(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(EmptyAgent)
    # Cancel listener so test can intercept agent responses
    await exchange_client._stop_listener_task()

    async with Runtime(
        EmptyAgent(),
        exchange_factory=exchange_client.factory(),
        registration=registration,
    ) as runtime:
        request = Message.create(
            src=exchange_client.client_id,
            dest=runtime.agent_id,
            body=ActionRequest(action='null'),
        )
        await exchange_client.send(request)
        message = await exchange_client._transport.recv()
        body = message.get_body()
        assert isinstance(body, ErrorResponse)
        exception = body.get_exception()
        assert isinstance(exception, AttributeError)
        assert 'null' in str(exception)


@pytest.mark.asyncio
async def test_bind_agent_handles_helper(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    class _TestAgent(Agent):
        def __init__(
            self,
            handle: Handle[EmptyAgent],
            proxy: ProxyHandle[EmptyAgent],
        ) -> None:
            super().__init__()
            self.direct = handle
            self.proxy = proxy
            self.sequence = HandleList([handle])
            self.mapping = HandleDict({'x': handle})

    factory = exchange_client.factory()
    registration = await exchange_client.register_agent(_TestAgent)
    proxy_handle = ProxyHandle(EmptyAgent())
    unbound_handle = UnboundRemoteHandle(
        (await exchange_client.register_agent(EmptyAgent)).agent_id,
    )

    async def _request_handler(_: Any) -> None:  # pragma: no cover
        pass

    async with await factory.create_agent_client(
        registration,
        _request_handler,
    ) as agent_client:
        agent = _TestAgent(unbound_handle, proxy_handle)
        _bind_agent_handles(agent, agent_client)

        assert agent.proxy is proxy_handle
        assert agent.direct.client_id == agent_client.client_id
        for handle in agent.sequence:
            assert handle.client_id == agent_client.client_id
        for handle in agent.mapping.values():
            assert handle.client_id == agent_client.client_id


class HandleBindingAgent(Agent):
    def __init__(
        self,
        unbound: UnboundRemoteHandle[EmptyAgent],
        agent_bound: RemoteHandle[EmptyAgent],
        self_bound: RemoteHandle[EmptyAgent],
    ) -> None:
        self.unbound = unbound
        self.agent_bound = agent_bound
        self.self_bound = self_bound

    async def agent_on_startup(self) -> None:
        assert isinstance(self.unbound, RemoteHandle)
        assert isinstance(self.agent_bound, RemoteHandle)
        assert isinstance(self.self_bound, RemoteHandle)

        assert isinstance(self.unbound.client_id, AgentId)
        assert self.unbound.client_id == self.agent_bound.client_id
        assert self.unbound.client_id == self.self_bound.client_id


@pytest.mark.asyncio
async def test_runtime_bind_handles(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    factory = exchange_client.factory()
    main_agent_reg = await exchange_client.register_agent(HandleBindingAgent)
    remote_agent1_reg = await exchange_client.register_agent(EmptyAgent)
    remote_agent1_id = remote_agent1_reg.agent_id
    remote_agent2_reg = await exchange_client.register_agent(EmptyAgent)

    async def _request_handler(_: Any) -> None:  # pragma: no cover
        pass

    main_agent_client = await factory.create_agent_client(
        main_agent_reg,
        _request_handler,
    )
    remote_agent2_client = await factory.create_agent_client(
        remote_agent2_reg,
        _request_handler,
    )

    agent = HandleBindingAgent(
        unbound=UnboundRemoteHandle(remote_agent1_id),
        agent_bound=RemoteHandle(remote_agent2_client, remote_agent1_id),
        self_bound=RemoteHandle(main_agent_client, remote_agent1_id),
    )

    # The agent is going to create it's own exchange client so we'd end up
    # with two clients for the same agent. Close this one as we just used
    # it to mock a handle already bound to the agent.
    await main_agent_client.close()

    async with Runtime(
        agent,
        exchange_factory=factory,
        registration=main_agent_reg,
    ) as runtime:
        # The self-bound remote handles should be ignored.
        assert runtime._exchange_client is not None
        assert len(runtime._exchange_client._handles) == 2  # noqa: PLR2004

    await remote_agent2_client.close()


class ShutdownAgent(Agent):
    @action
    async def end(self) -> None:
        self.agent_shutdown()


@pytest.mark.asyncio
async def test_runtime_agent_self_termination(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(ShutdownAgent)

    async with Runtime(
        ShutdownAgent(),
        exchange_factory=exchange_client.factory(),
        registration=registration,
    ) as runtime:
        await runtime.action('end', AgentId.new(), args=(), kwargs={})
        await runtime.wait_shutdown(timeout=TEST_WAIT_TIMEOUT)


class ContextAgent(Agent):
    @action(context=True)
    async def call(
        self,
        source_id: EntityId,
        *,
        context: ActionContext,
    ) -> None:
        assert source_id == context.source_id


@pytest.mark.asyncio
async def test_runtime_agent_action_context(
    exchange_client: UserExchangeClient[LocalExchangeTransport],
) -> None:
    registration = await exchange_client.register_agent(ShutdownAgent)

    async with Runtime(
        ContextAgent(),
        exchange_factory=exchange_client.factory(),
        registration=registration,
    ) as runtime:
        await runtime.action(
            'call',
            exchange_client.client_id,
            args=(exchange_client.client_id,),
            kwargs={},
        )
