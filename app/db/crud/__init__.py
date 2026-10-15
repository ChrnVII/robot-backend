"""数据访问层（CRUD 仓储）：对 db.models 的读写封装。

- workbench.py：ui_* 工作台表（任务 / 模板 / 运行 / 告警 / 预置点 / 配置 / 地图 / 升降）
- users.py：users / refresh_tokens 鉴权表

仅操作后端自有业务表；设备、控制、媒体、推理相关表由 robot-core 进程独占写入，
经 gateways → ipc 访问（检测结果查询只读 robot_platform 库）。
"""

from app.db.crud import users, workbench

__all__ = ["users", "workbench"]
