# 巡检机器人平台（纯 Python：FastAPI + robot_core 五进程）

按「方案 A」已将 old_project（Spring Boot + Python 混合）完整迁移为纯 Python 技术栈：

- **FastAPI 后端替换原 Java 后端角色**：REST、WebSocket、鉴权与任务编排；
- **机器人运行时保留多进程**：robot-core / sensor-worker / inference-worker / inspection-worker / sync-agent 独立进程运行，通过 Unix Domain Socket 上的 JSON Lines 契约通信；
- **Vue 前端契约不变**：统一响应封套、`X-Trace-Id`、错误码表、4 条 WebSocket 路径与消息格式。

## 运行架构

```text
Vue 工作台
  → FastAPI 后端（app/：REST / WebSocket / 鉴权 / 编排）
  → IPC（Unix Domain Socket，JSON Lines 一请求一响应；app/ipc）
  → robot-core（设备唯一写入者 / 租约 / 安全 / 状态）
       → sensor-worker（相机 / 热像 / 音频采集，媒体原子落盘）
       → inference-worker（受控模型包 / 隔离推理）
       → inspection-worker（实时巡检任务，单任务互斥）
       → sync-agent（PostgreSQL outbox，默认禁用）
  → PostgreSQL robot_platform + 本地媒体文件
```

机器人进程通过 Unix domain socket 通信（Linux 工控机）；联调需先启动 robot_core 五进程，见 [`deploy/README.md`](deploy/README.md)。

## 目录结构

```text
app/
  main.py                 FastAPI 装配（建表 / 默认管理员 / 中间件 / 路由挂载）
  api/
    router.py             /api/v1 汇总（auth 开放；其余挂 require_authentication）
    deps.py               依赖注入（网关 / 服务 / 数据库会话，进程内单例）
    validation.py         路径参数校验（对齐旧 @Pattern → 400）
    endpoints/            system auth robot control media inspection video workbench
    ws/                   hub.py 推送骨架 + routes.py 四个实时通道（500ms）
  common/                 config / logging / exceptions（错误码表）/ responses（封套）/ middleware（X-Trace-Id）/ clock
  security/               JWT（登录/刷新/登出/me）与角色依赖（默认关闭）
  gateways/               机器人进程网关：base（错误映射+camelize）/ robot_core / sensor_worker / inspection_worker
  ipc/                    UDS JSON Lines 协议（JsonSocketClient/Server，错误码透传）
  services/               robot / control / media / inspection（含检测分页）/ workbench（DB）
  schemas/                Pydantic 出入参（camelCase 线上契约）
  db/                     SQLAlchemy：database.py + models/ + crud/（workbench/users）
  robot_core/             机器人运行时域（迁自旧 runtime，独立进程代码）
    common contracts control devices navigation mission sensing
    inference inspection events storage sync diagnostics core
    processes/            robot_core / sensor_worker / inference_worker / inspection_worker / sync_agent
configs/                  base.yaml / dev.yaml（模拟，本机 PostgreSQL）/ hardware.yaml（生产 PG+硬件）
models/hs_detect/weights/ 热应激检测模型（best.pt，sha256 已配置）
tests/                    backend（后端接口/契约/WS/鉴权）+ robot_core（迁移的单元/契约/集成）
deploy/                   systemd 单元模板 + target；启动脚本；robot.env.example
```

分层与依赖规则：`api → services → gateways → ipc →（UDS）→ robot_core 进程`；
`api/services` 不得直接 import `robot_core` 内部实现；`db/crud` 只写后端自有表（`ui_*`、`users`）。

## 快速开始

```bash
uv sync                        # 全量依赖（一条命令装齐，含 torch / opencv / 串口）
uv run fastapi dev             # 开发服务器 http://127.0.0.1:8000（需同日启动 robot_core 五进程）
uv run pytest                  # 全量测试（118 项）

# 生产形态（沿用旧端口 8100）
uv run python -m uvicorn app.main:app --host 0.0.0.0 --port 8100

# 本机运行机器人五进程（模拟配置 + 本机 PostgreSQL，Linux/macOS 才有 UDS）
uv run python -m app.robot_core.processes.robot_core --config configs/dev.yaml --check
```

**约定：所有命令从项目根目录（本目录）运行**，`data/`、`run/` 相对路径按此解析。

数据库统一为 PostgreSQL（SQLite 支持已移除）：五进程连接 `configs/*.yaml` 中的 DSN，
后端与测试分别读取 `ROBOT_DATABASE_URL` 与 `ROBOT_TEST_DATABASE_URL`。

## 契约与错误码

- 统一封套：`{code, message, data, traceId, timestamp}`（成功 `code=OK`），`X-Trace-Id` 请求/响应透传；
- 错误映射对齐旧 `GlobalExceptionHandler`：`CORE_UNAVAILABLE→503`、`CORE_REJECTED→409`、
  `INVALID_DEVICE_ID→409`、`SENSOR_UNAVAILABLE→503`、`CAMERA_CAPTURE_ERROR/AUDIO_CAPTURE_ERROR→409`、
  `KEY_ERROR/FILE_NOT_FOUND_ERROR→404`、`VALIDATION_ERROR/INVALID_JSON→400`、`INTERNAL_ERROR→500`；
- 工作台接口返回自由 JSON（兼容旧内存态契约）：`/tasks`、`/task-templates`、`/runs`、`/alerts`、
  `/config?configType=`、`/map/stations`、`/cam/presets`、`/lift/state`（已落库持久化）；
- WebSocket：`/ws/state`、`/ws/environment`、`/ws/inspections/{taskRunId}`、`/ws/video/{streamId}?cameraId=`，
  消息 `{"type","data","timestamp"}`，500ms 推送，非法 ID 以 1007 关闭。

## 鉴权（security/）

默认 `ROBOT_AUTH_ENABLED=false`（与旧前端联调契约一致）。生产开启后 `/api/v1/**`（除 `/auth/*`）
要求 `Authorization: Bearer <accessToken>`；`/healthz`、`/readyz`、`/version` 保持公开。
端点：`POST /api/v1/auth/login|refresh|logout`、`GET /api/v1/users/me`；
角色权限工厂 `require_roles(...)` 可挂到需细粒度授权的路由。详见 `deploy/README.md`。

## 数据库

| 库 | 内容 | 写入方 |
|---|---|---|
| `ROBOT_DATABASE_URL` | 后端自有表：`ui_*` 工作台、`users`、`refresh_tokens`；只读 `inference_runs` / `media_assets` / `inference_detections` | 后端（crud 显式提交）、robot-core（检测记录） |

生产环境后端与 robot-core 共用同一个 `robot_platform` 实例，因此只配置这一个 DSN：
检测分页（`InferenceQueryService`）复用后端共享 engine，不再单独开第二个连接池。
后端用 `create_all` 启动建表；runtime 用 `schema.sql`（版本 6），版本一致时启动不再重跑 DDL。

## 模型与推理

`configs/hardware.yaml` 指向 `models/hs_detect/weights/best.pt`（sha256 已校验一致）。
依赖默认全量安装（`pyproject.toml` 的 `default-groups`）：一条 `uv sync` 装齐
torch / ultralytics / opencv / pyserial。精简场景按组排除：
`uv sync --no-group inference`（跳过 torch），`media` / `hardware` 同理。

## 部署

systemd（五进程 + 后端 target）方案（含同步命令、目录准备与模型权重说明）见 [`deploy/README.md`](deploy/README.md)。

## 与旧工程的差异

| 项 | 说明 |
|---|---|
| robot-api 过渡进程 | 不再迁移（对外 API 由本 FastAPI 后端取代） |
| legacy 兼容层（Flask robot_dataflow / legacy_compat） | 未迁移（按旧文档定位：仅迁移对照材料） |
| 实车能力 | 硬件协议、HIL、真实模型验收、云同步按旧平台约定仍待 M2-M6 推进 |
| 新增变更 | 工作台数据由内存态改为 DB 持久化；新增 JWT 鉴权（默认关闭）；模型包路径改为 `models/` |

## 测试

测试运行在 PostgreSQL 测试库上（`ROBOT_TEST_DATABASE_URL`，默认 `…/robot_platform_test`，
每个用例重建 schema 隔离；数据库不可用时整组跳过）。

```bash
uv run pytest                    # 全部（robot_core 迁移用例 + 后端用例）
uv run pytest tests/backend -v   # 后端接口/契约/WS/鉴权
uv run pytest tests/robot_core   # 机器人内核（租约/安全/存储/巡检/同步）
```

覆盖：封套与 TraceId、网关全接口、命令幂等与急停副作用、检测分页（真实 PostgreSQL 联调）、
工作台 CRUD 与版本冲突、鉴权全流程、WebSocket 四通道与非法 ID、UDS 协议（Linux）、
租约冲突/命令过期/运动限制等内核契约。
