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

    def reset(self) -> None:
        """解除急停锁。

        只允许 RobotCore.reset_emergency 在校验过复位请求后调用：急停是锁存语义，
        不能因为下一条普通命令顺带恢复，否则「急停后设备自己又动了」。
        """
        self.emergency = False

    @staticmethod
    def is_stop_command(command: DeviceCommand) -> bool:
        """是否为停止类动作（含云台 direct=stop）。停止永远放行，见 validate。"""
        if command.action == "stop":
            return True
        return (
            command.target == "camera"
            and command.action == "camera.ptz"
            and str(command.payload.get("direct", "")).lower() == "stop"
        )

    def validate(self, command: DeviceCommand) -> None:
        is_stop = self.is_stop_command(command)
        # 停止不校验过期：TTL 只约束「开始动作」，一条迟到的停止仍然是安全的，
        # 而拒绝它会让云台/升降停在动作中途。这是 stop 与其它命令的关键差异。
        if not is_stop and parse_utc(command.expires_at) <= parse_utc(utc_now()):
            raise CommandRejected("command_expired")
        is_camera = command.target == "camera"
        camera_during_emergency = self.emergency and is_camera
        if not is_stop and not camera_during_emergency and not self.leases.validate(command.lease_id):
            raise CommandRejected("control_lease_invalid")
        if self.emergency and not is_camera and not is_stop:
            raise CommandRejected("emergency_active")
        if command.target == "motion" and command.action == "move":
            distance = abs(float(command.payload.get("distance_m", 0.0)))
            if distance > self.max_motion_distance_m:
                raise CommandRejected("motion_limit_exceeded")

