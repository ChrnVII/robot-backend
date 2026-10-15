"""机器人运行时域（独立进程代码，迁自 old_project/runtime 的 inspection_robot 包）。

- 五进程入口位于 app.robot_core.processes：robot_core / sensor_worker /
  inference_worker / inspection_worker / sync_agent；
- api / services 不得直接 import 本包内部实现，只能经 app.gateways → app.ipc 访问；
- IPC 协议层已提升为共享模块 app.ipc。
"""

__version__ = "0.2.0"
