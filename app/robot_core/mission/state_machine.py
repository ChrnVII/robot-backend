from __future__ import annotations

from typing import Dict, Set

from app.robot_core.contracts import Event
from app.robot_core.storage import PlatformStore


TRANSITIONS: Dict[str, Set[str]] = {
    "booting": {"self_checking", "emergency"},
    "self_checking": {"idle", "fault", "emergency"},
    "idle": {"manual", "executing", "emergency"},
    "manual": {"idle", "emergency"},
    "executing": {"paused", "returning", "fault", "emergency"},
    "paused": {"executing", "returning", "emergency"},
    "returning": {"charging", "fault", "emergency"},
    "charging": {"idle", "fault", "emergency"},
    "fault": {"self_checking", "emergency"},
    "emergency": {"self_checking"},
}


class InvalidTransition(RuntimeError):
    pass


class MissionStateMachine:
    def __init__(self, store: PlatformStore, state: str = "booting"):
        self.store = store
        self.state = state

    def transition(self, target: str, *, reason: str, actor: str, trace_id: str,
                   command_id: str = "", snapshot: dict = None) -> str:
        if target not in TRANSITIONS.get(self.state, set()):
            raise InvalidTransition(f"transition {self.state} -> {target} is not allowed")
        previous = self.state
        self.state = target
        self.store.append_event(Event("robot.state_changed", actor, trace_id, {
            "from": previous, "to": target, "reason": reason,
            "command_id": command_id, "snapshot": dict(snapshot or {}),
        }))
        return self.state

