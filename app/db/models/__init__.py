"""SQLAlchemy ORM 模型：仅后端自有业务表（工作台 UI 数据与鉴权）。

设计说明：旧 Java UiApplicationService 以「Map 通用结构 + JSON 文件」存储工作台数据；
本迁移保留同样的自由结构（payload JSON 列），确保前端契约零改动，同时落库持久化。
控制表归 robot-core，媒体、巡检与推理表归感知进程；结构统一由 app.migrations 管理。
"""

from sqlalchemy import JSON, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class TaskRecord(Base):
    """工作台任务（/api/v1/tasks）。"""

    __tablename__ = "ui_tasks"

    task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[str] = mapped_column(String(64), default="")


class TaskTemplate(Base):
    """默认任务模板（/api/v1/task-templates）。"""

    __tablename__ = "ui_task_templates"

    template_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[str] = mapped_column(String(64), default="")


class UiRun(Base):
    """执行记录（/api/v1/runs）。"""

    __tablename__ = "ui_runs"

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[str] = mapped_column(String(64), default="")


class UiAlert(Base):
    """告警（/api/v1/alerts）。"""

    __tablename__ = "ui_alerts"

    alert_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    level: Mapped[str] = mapped_column(String(32), default="info")
    status: Mapped[str] = mapped_column(String(32), default="open")
    message: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[str] = mapped_column(String(64), default="")


class Preset(Base):
    """摄像头预置点（/api/v1/cam/presets）。"""

    __tablename__ = "ui_presets"

    preset_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[str] = mapped_column(String(64), default="")


class ConfigEntry(Base):
    """工作台参数键值（/api/v1/config）。"""

    __tablename__ = "ui_config"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, nullable=True)


class MapState(Base):
    """地图站点与版本（/api/v1/map/stations）。"""

    __tablename__ = "ui_map"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    map_version: Mapped[int] = mapped_column(Integer, default=1)
    stations: Mapped[list] = mapped_column(JSON, default=list)


class LiftState(Base):
    """升降状态快照（/api/v1/lift/state）。"""

    __tablename__ = "ui_lift"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    state: Mapped[dict] = mapped_column(JSON, default=dict)


class User(Base):
    """用户（鉴权）。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(64), default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(String(64), default="")


class RefreshToken(Base):
    """刷新令牌（可撤销）。"""

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    expires_at: Mapped[str] = mapped_column(String(64))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(64), default="")


# ---------------------------------------------------------------------------
# 巡检任务（采集会话）。与 ui_* 影子表并存但互不写入：
# ui_* 服务旧工作台页面，本组表承载新任务链路（missions / mission_points / mission_runs）。
# 结构由 app/migrations 管理，字段命名与 robot-core 的 schema.sql 保持一致。
# ---------------------------------------------------------------------------


class Mission(Base):
    """巡检任务定义。采集配置（视频/音频/时长）整体存在 definition JSON 里。"""

    __tablename__ = "missions"

    mission_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    topology_revision_id: Mapped[str | None] = mapped_column(String(128))
    definition: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    created_at: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[str | None] = mapped_column(String(64))


class MissionPoint(Base):
    """任务站点序列（一期可空，二期填真实笼位）。"""

    __tablename__ = "mission_points"

    mission_point_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    mission_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    seq_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cage_id: Mapped[str] = mapped_column(String(128), nullable=False)


class MissionRun(Base):
    """任务运行实例（一次采集会话）。params_json 记录本次实际生效的采集组合。"""

    __tablename__ = "mission_runs"

    task_run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    mission_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    robot_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    current_step: Mapped[str | None] = mapped_column(String(64))
    current_waypoint_id: Mapped[str | None] = mapped_column(String(128))
    config_revision_id: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    finished_at: Mapped[str | None] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    recovery_state: Mapped[str | None] = mapped_column(String(64))
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
