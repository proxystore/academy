# Getting Started

## Installation

You can install Academy with `pip` or from source.
We suggest installing within a virtual environment (e.g., `venv` or Conda).
```bash
python -m venv venv
. venv/bin/activate
```

*Option 1: Install from PyPI:*
```bash
pip install academy-py
```

*Option 2: Install from source:*
```bash
git clone git@github.com:proxystore/academy
cd academy
pip install -e .  # -e for editable mode
```

## A Basic Example

The following script defines, initializes, and launches a simple agent that performs a single action.
Click on the plus (`+`) signs to learn more.

```python title="example.py" linenums="1"
from academy.behavior import Behavior, action
from academy.exchange.thread import ThreadExchangeFactory
from academy.launcher import ThreadLauncher
from academy.logging import init_logging
from academy.manager import Manager

class ExampleAgent(Behavior):  # (1)!
    @action  # (2)!
    def square(self, value: float) -> float:
        return value * value

def main() -> None:
    init_logging('INFO')

    with Manager(  # (3)!
        exchange=ThreadExchangeFactory(),  # (4)!
        launcher=ThreadLauncher(),  # (5)!
    ) as manager:
        agent_handle = manager.launch(ExampleAgent())  # (6)!

        future = agent_handle.square(2)  # (7)!
        assert future.result() == 4

        agent_handle.shutdown()  # (8)!

if __name__ == '__main__':
    main()
```

1. Running agents implement a [`Behavior`][academy.behavior.Behavior].
2. Behavior methods decorated with [`@action`][academy.behavior.action] can be invoked remotely by clients and other agents. An agent can call action methods on itself as normal methods.
3. The [`Manager`][academy.manager.Manager] is a high-level interface that reduces boilerplate code when launching and managing agents. It will also manage clean up of resources and shutting down agents when the context manager exits.
4. The [thread exchange][academy.exchange.thread.ThreadExchangeFactory] manages message passing between clients and agents running in different threads of a single process.
5. The [`ThreadLauncher`][academy.launcher.ThreadLauncher] launches agents in threads of the current process.
6. An instantiated behavior (here, `ExampleAgent`) can be launched with [`Manager.launch()`][academy.manager.Manager.launch], returning a handle to the remote agent.
7. Interact with running agents via a [`BoundRemoteHandle`][academy.handle.BoundRemoteHandle]. Invoking an action returns a future to the result.
8. Agents can be shutdown via a handle or the manager.

Running this script with logging enabled produces the following output:
```
$ python example.py
INFO (root) Configured logger (stdout-level=INFO, logfile=None, logfile-level=None)
INFO (academy.manager) Initialized manager (ClientID<6e890226>; ThreadExchange<4401447664>)
INFO (academy.manager) Launched agent (AgentID<ad6faf7e>; Behavior<ExampleAgent>)
INFO (academy.agent) Running agent (AgentID<ad6faf7e>; Behavior<ExampleAgent>)
INFO (academy.agent) Shutdown agent (AgentID<ad6faf7e>; Behavior<ExampleAgent>)
INFO (academy.manager) Closed manager (ClientID<6e890226>)
```

## Control Loops

Control loops define the autonomous behavior of a running agent and are created by decorating a method with [`@loop`][academy.behavior.loop].

```python
import threading
import time
from academy.behavior import loop

class ExampleAgent(Behavior):
    @loop
    def counter(self, shutdown: threading.Event) -> None:
        count = 0
        while not shutdown.is_set():
            print(f'Count: {count}')
            count += 1
            time.sleep(1)
```

All control loops are started in separate threads when an agent is executed, and run until the control loop exits or the agent is shut down, as indicated by the `shutdown` event.

## Agent to Agent Interaction

Agent handles can be passed to other agents to facilitate agent-to-agent interaction.
Here, a `Coordinator` is initialized with handles to two other agents implementing the `Lowerer` and `Reverser` behaviors, respectively.

```python
from academy.behavior import action
from academy.behavior import Behavior
from academy.handle import Handle

class Coordinator(Behavior):
    def __init__(
        self,
        lowerer: Handle[Lowerer],
        reverser: Handle[Reverser],
    ) -> None:
        self.lowerer = lowerer
        self.reverser = reverser

    @action
    def process(self, text: str) -> str:
        text = self.lowerer.action('lower', text).result()
        text = self.reverser.action('reverse', text).result()
        return text

class Lowerer(Behavior):
    @action
    def lower(self, text: str) -> str:
        return text.lower()

class Reverser(Behavior):
    @action
    def reverse(self, text: str) -> str:
        return text[::-1]
```

After launching the `Lowerer` and `Reverser`, the respective handles can be used to initialize the `Coordinator` before launching it.

```python
from academy.exchange.thread import ThreadExchange
from academy.launcher import ThreadLauncher
from academy.logging import init_logging
from academy.manager import Manager

def main() -> None:
    init_logging('INFO')

    with Manager(
        exchange=ThreadExchange(),
        launcher=ThreadLauncher(),
    ) as manager:
        lowerer = manager.launch(Lowerer())
        reverser = manager.launch(Reverser())
        coordinator = manager.launch(Coordinator(lowerer, reverser))

        text = 'DEADBEEF'
        expected = 'feebdaed'

        future = coordinator.process(text)
        assert future.result() == expected

if __name__ == '__main__':
    main()
```


## Distributed Execution

The prior examples have launched agent in threads of the main process, but in practice agents are launched in different processes, possibly on the same node or remote nodes.
The prior example can be executed in a distributed fashion by changing the launcher and exchange to implementations which support distributed execution.
Below, a [Redis server](https://redis.io/){target=_blank} server (via the [redis exchange][academy.exchange.redis.RedisExchangeFactory]) is used to support messaging between distributed agents executed with a [`ProcessPoolExecutor`][concurrent.futures.ProcessPoolExecutor] (via the [`Launcher`][academy.launcher.Launcher]).

```python
from concurrent.futures import ProcessPoolExecutor
from academy.exchange.redis import RedisExchangeFactory
from academy.launcher import Launcher

def main() -> None:
    process_pool = ProcessPoolExecutor(max_processes=4)
    with Manager(
        exchange=RedisExchangeFactory('<REDIS HOST>', port=6379),
        launcher=Launcher(process_pool),
    ) as manager:
        ...
```

The [`Launcher`][academy.launcher.Launcher] is compatible with any [`concurrent.futures.Executor`][concurrent.futures.Executor].
