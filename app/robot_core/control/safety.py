from __future__ import annotations

from app.robot_core.common import parse_utc, utc_now
from app.robot_core.contracts import DeviceCommand

from .leases import LeaseManager


class CommandRejected(RuntimeError):
    pass


class SafetySupervisor:
    def __init__(self, leases: LeaseManager, max_motion_distance_m: float = 2.0):
        self.leases = leases
        self.max_motion_distance_m = max_motion_distance_m
        self.emergency = False

    def validate(self, command: DeviceCommand) -> None:
        if parse_utc(command.expires_at) <= parse_utc(utc_now()):
            raise CommandRejected("command_expired")
        if not self.leases.validate(command.lease_id):
            raise CommandRejected("control_lease_invalid")
        if self.emergency and command.action != "stop":
            raise CommandRejected("emergency_active")
        if command.target == "motion" and command.action == "move":
            distance = abs(float(command.payload.get("distance_m", 0.0)))
            if distance > self.max_motion_distance_m:
                raise CommandRejected("motion_limit_exceeded")

