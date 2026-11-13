"""Online session routing without transport, persistence or business dependencies."""

from collections.abc import Callable


class NotificationHub:
    def __init__(self):
        self.sinks: dict[str, Callable[[dict], None]] = {}
        self.owners: dict[str, str] = {}

    def connect(self, session_id: str, sink: Callable[[dict], None]) -> None:
        self.sinks[session_id] = sink

    def disconnect(self, session_id: str) -> None:
        self.sinks.pop(session_id, None)
        self.owners = {key: owner for key, owner in self.owners.items() if owner != session_id}

    def claim(self, operation_id: str, session_id: str) -> bool:
        if session_id not in self.sinks:
            return False
        return self.owners.setdefault(operation_id, session_id) == session_id

    def notify(self, session_id: str, value: dict) -> None:
        if sink := self.sinks.get(session_id):
            sink(value)

    def publish(self, value: dict) -> None:
        # Recipients are fixed at publication; a sink may disconnect while enqueuing.
        for sink in tuple(self.sinks.values()):
            sink(value)
