---
name: 'build_a_stdlib_only_an_in_process_event_bus_pub'
category: 'autonomous'
description: 'Build a stdlib-only an in-process event bus / pub-sub dispatcher with type hints, docstring and an assert self-test printing OK.'
triggers: ["build a stdlib only an in process event bus pub"]
version: '1.0'
author: 'Genesis'
last_updated: '2026-07-27T13:53:30.095292+00:00'
---

## Описание
Build a stdlib-only an in-process event bus / pub-sub dispatcher with type hints, docstring and an assert self-test printing OK.

## Python Код
```python
"""In‑process event bus / pub‑sub dispatcher.

Provides type‑checked subscription based on the *type* of the event object.
Handlers receive the event instance directly.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Type


class EventBus:
    """Simple synchronous event bus.

    Handlers are registered per event *type* (class). When an event instance is
    published, all handlers subscribed to its type are invoked with the event
    as the sole argument.

    Example
    -------
    >>> bus = EventBus()
    >>> bus.subscribe(MyEvent, lambda e: print(e.value))
    >>> bus.publish(MyEvent(42))
    42
    """

    _handlers: Dict[Type[Any], List[Callable[[Any], None]]]

    def __init__(self) -> None:
        """Create an empty bus."""
        self._handlers = {}

    def subscribe(self, event_type: Type[Any], handler: Callable[[Any], None]) -> None:
        """Register *handler* for events of *event_type*.

        Parameters
        ----------
        event_type: type
            The class of events the handler is interested in.
        handler: Callable[[Any], None]
            Callable that accepts a single argument – the event instance.
        """
        self._handlers.setdefault(event_type, []).append(handler)

    def publish(self, event: Any) -> None:
        """Dispatch *event* to all handlers subscribed to its type.

        Parameters
        ----------
        event: Any
            Instance of the event to be delivered.
        """
        for handler in self._handlers.get(type(event), []):
            handler(event)


# --------------------------------------------------------------------------- #
# Self‑test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    # Define a simple event type
    class TestEvent:
        def __init__(self, payload: str) -> None:
            self.payload = payload

    # Flag that will be mutated by the handler
    flag = {"called": False, "value": None}

    def handler(ev: TestEvent) -> None:
        flag["called"] = True
        flag["value"] = ev.payload

    bus = EventBus()
    bus.subscribe(TestEvent, handler)

    # Publish an event and verify the handler was invoked correctly
    bus.publish(TestEvent("hello world"))
    assert flag["called"] is True
    assert flag["value"] == "hello world"

    # Publishing an event with no subscribers must be a no‑op, not raise
    class UnsubscribedEvent:  # noqa: D401
        """An event type with no listeners."""
        pass

    bus.publish(UnsubscribedEvent())  # should silently do nothing

    print("OK")
```

## Pitfalls
- OK

