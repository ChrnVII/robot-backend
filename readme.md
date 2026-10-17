# 巡检机器人平台（FastAPI + 控制进程 + 感知进程）

- **FastAPI 后端**（`app/`）：REST、WebSocket、鉴权与任务编排；
- **机器人运行时**（`app/robot_core/`）：robot-core 管控制，inspection-worker 管采集、模型和巡检；sync-agent 按需启用，通过 Unix Domain Socket 的 JSON Lines 契约通信；
- **Vue 前端契约**：统一响应封套、`X-Trace-Id`、错误码表、4 条 WebSocket 路径与消息格式。

## 运行架构

```text
Vue 工作台
  → FastAPI 后端（app/：REST / WebSocket / 鉴权 / 编排）
  → IPC（Unix Domain Socket，JSON Lines 一行一请求；客户端连接池复用；app/ipc）
  → robot-core（设备控制唯一写入者 / 租约 / 急停 / 状态缓存）
  → inspection-worker（相机与音频采集 / 模型复用 / 巡检 / 证据持久化）
       同时提供 inspection-worker.sock 与原 sensor-worker.sock
  → sync-agent（PostgreSQL outbox，按需启用）
  → PostgreSQL robot_platform + 本地媒体文件
      ↑ 控制与感知写入时 NOTIFY robot_events → 后端 LISTEN → WS 主题即时刷新
```

默认共三个常驻进程。原 sensor-worker / inference-worker CLI 保留供单独调试，勿与感知进程同时启动。部署见 [`deploy/README.md`](deploy/README.md)。

## 目录结构

```text
app/
  main.py                 FastAPI 装配（迁移检查 / 默认管理员 / 中间件 / 路由挂载 / 生命周期）
  api/
    router.py             /api/v1 汇总（auth 开放；其余挂 require_authentication）
    deps.py               依赖注入（网关 / 服务 / 数据库会话，进程内单例）
    validation.py         路径参数校验（非法格式 → 400）
    endpoints/            system auth robot control media inspection video workbench
    ws/                   hub.py 推送中枢 + routes.py 四个实时通道 + events.py PG 事件监听
  contracts/              【共享契约核，零依赖】errors（稳定错误码）+ wire（操作名与载荷模型）
  common/                 config / logging / exceptions（错误码表）/ responses（封套）/ middleware（X-Trace-Id）/ clock / dburl
  security/               JWT（登录/刷新/登出/me）与角色依赖（默认关闭）
  gateways/               机器人进程网关：base（错误映射+camelize）/ robot_core / sensor_worker / inspection_worker
  ipc/                    UDS JSON Lines 协议（JsonSocketClient 连接池 / JsonSocketServer 多请求复用）
  services/               robot / control / media / inspection（含检测分页）/ workbench（DB）
  schemas/                Pydantic 出入参（camelCase 线上契约）
  db/                     SQLAlchemy：database.py + models/ + crud/（workbench/users）
  migrations/             Alembic 迁移历史（API 与运行时共用）、部署角色授权
  robot_core/             机器人运行时域（独立进程代码）
    common contracts control devices navigation mission sensing
    inference inspection events storage sync diagnostics core
    common/camera_http.py 相机 HTTP 基建（控制与采集共用：认证 / 脱敏）
    processes/            robot_core / sensor_worker / inference_worker / inspection_worker / sync_agent
                          （inspection_worker 的任务引擎为 inspection_engine.py）
configs/                  base.yaml / dev.yaml（模拟，本机 PostgreSQL）/ hardware.yaml（生产 PG+硬件）
models/hs_detect/weights/ 热应激检测模型（best.pt，sha256 已配置）
tests/                    backend（后端接口/契约/WS/鉴权）+ robot_core（单元/契约/集成）
deploy/                   systemd 单元模板 + target；启动脚本；robot.env.example
```

分层与依赖规则：`api → services → gateways → ipc →（UDS）→ robot_core 进程`；
`api/services` 不得直接 import `robot_core` 内部实现；`db/crud` 只写后端自有表（`ui_*`、`users`）。
唯一例外是 `app/contracts`——双端共用的零依赖契约核，两侧都可 import（见「契约核」）。

## 快速开始

```bash
uv sync                        # 全量依赖（一条命令装齐，含 torch / opencv / 串口）
uv run fastapi dev             # 开发服务器 http://127.0.0.1:8000（需先启动控制与感知进程）
uv run pytest                  # 全量测试

# 生产形态（端口 8100）
uv run python -m uvicorn app.main:app --host 0.0.0.0 --port 8100

# 模拟配置 + 本机 PostgreSQL（Linux/macOS 才有 UDS）
uv run python -m app.robot_core.storage.migrate --config configs/dev.yaml upgrade
uv run python -m app.robot_core.processes.robot_core --config configs/dev.yaml --check
```

**约定：所有命令从项目根目录（本目录）运行**，`data/`、`run/` 相对路径按此解析。

数据库为 PostgreSQL：后端读 `ROBOT_DATABASE_URL`，测试读 `ROBOT_TEST_DATABASE_URL`（见「数据库」「测试」）。

## 契约与错误码

- 统一封套：`{code, message, data, traceId, timestamp}`（成功 `code=OK`），`X-Trace-Id` 请求/响应透传；
- 错误码 → HTTP 状态映射：`CORE_UNAVAILABLE→503`、`CORE_REJECTED→409`、
  `INVALID_DEVICE_ID→409`、`SENSOR_UNAVAILABLE→503`、`CAMERA_CAPTURE_ERROR/AUDIO_CAPTURE_ERROR→409`、
  `NOT_FOUND（兼容旧 KeyError/FileNotFoundError）→404`、`INVALID_REQUEST→400`、
  `VALIDATION_ERROR/INVALID_JSON→400`、`INTERNAL_ERROR→500`；
- 工作台接口返回自由 JSON：`/tasks`、`/task-templates`、`/runs`、`/alerts`、
  `/config?configType=`、`/map/stations`、`/cam/presets`、`/lift/state`（数据持久化于 PostgreSQL）；
- WebSocket：`/ws/state`、`/ws/environment`、`/ws/inspections/{taskRunId}`、`/ws/video/{streamId}?cameraId=`，
  消息 `{"type","data","timestamp"}`，非法 ID 以 1007 关闭；鉴权开启时需携带访问令牌
  （查询参数 `accessToken` 或 `Authorization: Bearer`），缺失/非法以 1008 关闭。

## 契约核（app/contracts/）

跨进程契约的单一事实来源，**必须保持零依赖**（不得 import `app.*` 其他子包）：

- `IpcErrorCode` + `ipc_error_code()`：稳定错误码，替代历史上「异常类名即错误码」的透传
  （`KeyError → NOT_FOUND`、`ValueError/TypeError → INVALID_REQUEST`，未归类 → `INTERNAL_ERROR`）；
  消费侧 `is_not_found_code()` 同时接受旧类名，滚动升级期间新旧混布安全；
- `Op` / `Channels` / `EventTopic`：operation 名、NOTIFY 通道名、事件主题常量；
- `CommandWire` / `LeaseAcquireWire` / `LeaseReleaseWire` / `EmergencyStopWire`：
  控制域 IPC 载荷模型。后端用 `to_payload()` 组装、robot_core 用 `from_mapping()` 解析，
  字段拼写错误在解析时抛 `WireError`（→ 400），而非深层随机 KeyError。

## 实时推送与 IPC

- **IPC 连接池**：`JsonSocketClient` 复用 UDS 连接（池上限 8），连接/写/读全链路超时；
  遇陈旧连接（旧版对端响应一次即关闭）自动丢弃并重试一次；服务端单连接内循环处理多个请求，
  并有并发上限（64）与请求/响应大小上限。应用关闭时经 `gateway.aclose()` 释放池。
- **WS 推送中枢**（`api/ws/`）：同一主题共享一次快照 IPC，按 cameraId 隔离视频订阅；
  每个订阅者独立发送，队列只保留最新消息，慢客户端不会拖住其他客户端。
- **图片**：内部 IPC 传本地 JPEG 引用，API 输出仍为原 Base64 字段；预览每相机保留 16 帧，重启清理旧预览。
  历史证据原图与结果图保留，未自动删除；运维需监控磁盘并归档历史任务。
- **事件驱动**：控制与感知进程在设备状态采样、命令完成、逐帧推理落库时于事务内
  `NOTIFY robot_events`（见 `storage/database.py`），后端 `PgEventListener`
  LISTEN 后唤醒对应主题立即刷新（带防抖与最小刷新间隔）；500ms 周期作为兜底，
  数据库不可用或 `ROBOT_WS_EVENT_LISTENER_ENABLED=false` 时自动降级为纯周期推送。

## 鉴权（security/）

默认 `ROBOT_AUTH_ENABLED=false`。开启后 `/api/v1/**`（除 `/auth/*`）要求
`Authorization: Bearer <accessToken>`；`/healthz`、`/readyz`、`/version` 保持公开。
端点：`POST /api/v1/auth/login|refresh|logout`、`GET /api/v1/users/me`；
角色权限工厂 `require_roles(...)` 可挂到需细粒度授权的路由。详见 `deploy/README.md`。

## 数据库

仅支持 PostgreSQL；三个进程共用 `robot_platform` 库，可分别配置受限账号。

| 表 | 写入方 |
|---|---|
| `ui_*` 工作台、`users`、`refresh_tokens` | 后端（crud 显式提交） |
| `commands` / `control_leases` / 设备状态 | robot-core |
| `inspection_runs` / `inference_runs` / `media_assets` / `inference_detections` | 感知进程 |
| `events` / `outbox` | 各生产者与业务变更同事务写入；sync-agent 负责投递 |

后端检测分页读取 `v_inference_run_results`、`v_inference_detections` 视图；
每页固定最多三次查询，不随检测数量增加。数据库故障返回原错误封套，未初始化开发库保留空页兼容行为。

DSN：后端 `ROBOT_DATABASE_URL`；控制、感知、同步分别支持
`ROBOT_CORE_DATABASE_URL`、`ROBOT_PERCEPTION_DATABASE_URL`、`ROBOT_SYNC_DATABASE_URL`，
未设置则沿用 YAML 的 `storage.database_url`。迁移命令支持 `ROBOT_MIGRATION_DATABASE_URL`。

表结构唯一来源为 `app/migrations`；兼容旧 v1–v7 数据库，当前版本 8。
`storage/schema.sql` 与迁移中的初始 SQL 均为 v7 基线，不能作为当前建库入口。
开发默认自动迁移；生产设置 `ROBOT_AUTO_MIGRATE=false`，部署时单独执行 `upgrade`，
应用只检查版本。`check` / `--check` 不执行数据库迁移。迁移与最小权限配置见部署说明。

急停先向运动和升降设备并发发送停止，数据库审计在后台补写；数据库故障时本地审计文件保留供重放。
健康与状态读取缓存。巡检生命周期由感知进程统一持久化，重启将中断任务标记为原有 `failed` 状态，保留进度；
工作台只在设备确认成功后更新状态。任务并不自动续跑。

## 模型与推理

`configs/hardware.yaml` 指向 `models/hs_detect/weights/best.pt`（sha256 锁定于配置）。

## 部署

systemd（三进程 target，同步可选）方案见 [`deploy/README.md`](deploy/README.md)。

### 前后端同机部署（要求）

**本后端应与前端（`frontend/`，Vue 工作台）部署在同一台主机上。** 


## 边界与后续

- 旧工程的 robot-api 过渡进程与 Flask legacy 兼容层不在本仓库范围（对外 API 由本后端承担）；
- 实车能力（硬件协议验收、HIL、真实模型验收、云同步）按里程碑 M2-M6 推进。

## 测试

测试运行在 PostgreSQL 测试库上（`ROBOT_TEST_DATABASE_URL`，默认 `…/robot_platform_test`，
每个用例重建 schema 隔离；数据库不可用时跳过依赖数据库的用例）。
标记 `@pytest.mark.no_database` 的纯单元用例（契约核 / WS 中枢 / IPC 帧协议）不依赖数据库，
在任意机器上都会执行（UDS 用例在非 POSIX 平台跳过）。

```bash
uv run pytest                    # 全部（robot_core 用例 + 后端用例）
uv run pytest tests/backend -v   # 后端接口/契约/WS/鉴权
uv run pytest tests/robot_core   # 机器人内核（租约/安全/存储/巡检/同步）
```

覆盖：封套与 TraceId、网关全接口、命令幂等与急停副作用、检测分页（真实 PostgreSQL 联调）、
工作台 CRUD 与版本冲突、鉴权全流程、WebSocket 四通道与非法 ID、UDS 协议（Linux）、
租约冲突/命令过期/运动限制等内核契约；新增覆盖 IPC 连接复用与陈旧连接重试、
契约核字段往返、WS 主题扇出/事件唤醒/防抖/失败容错。

### 球形摄像头实时预览与录像

后端 web 进程使用一路 RTSP，同时向浏览器推送 Annex-B 码流，并按需原样转封装为 MKV；不重编码。此次只实现预览与录像，既有实时检测配置默认关闭。原有云台与预置点接口继续独立工作。

在 `robot.env` 配置 `ROBOT_CAMERA_STREAM_URL=rtsp://<user>:<encoded-password>@<host>:554/Streaming/Channels/102`，建议子码流使用 H.264。浏览器低延迟预览需要 Chrome / Edge，并通过 HTTPS 或 localhost 访问；不具备解码条件时显示已鉴权的 JPEG 快照预览。

录像在页面手动开始/停止，保存到 `ROBOT_CAMERA_RECORDINGS_DIR`（默认 `data/camera-recordings`，相对于后端工作目录）。起录等待关键帧，停止时排空队列并关闭容器；离开页面仍继续录制。RTSP 断线时结束当前文件，重连后从关键帧另开文件。写盘错误或队列积压时停止录像并报告错误，避免静默丢失参考帧。

接口（沿用 `/api/v1` 统一鉴权）：
- `GET /api/v1/cameras`：相机目录（见下节），前端据此渲染通道。
- `GET /api/v1/cam/stream/status`：连接诊断及 `recording` 状态。
- `GET /api/v1/cam/recordings`：文件列表，`active` 标识正在写入的文件。
- `POST /api/v1/cam/recordings`：`{"action":"start"}` 或 `{"action":"stop"}`。
- `GET /api/v1/cam/recordings/{name}`：下载已收尾的 MKV。
- `DELETE /api/v1/cam/recordings/{name}`：删除已收尾的 MKV。
- 以上 `/cam/*` 接口均接受可选 `cameraId`（缺省为球形摄像头），非球形或未配置的编号返回可读错误。

正在写入的文件禁止下载和删除。文件持续录制到手动停止或连接中断，请按需维护录像目录的可用空间。

无实机回归：`uv run pytest tests/backend/test_camera_stream.py tests/backend/test_camera_recording.py`。测试通过本地生成的 H.264 编码数据验证 MKV 落盘、解码回读和时间间隔，并覆盖启停、断线分段、写盘异常和文件接口；摄像头实机延迟与兼容性仍需现场验收。

### 相机编号与相机目录

一套相机在三层各有名字，这里统一为**相机编号（cameraId）**：

| 层 | 球形摄像头 | 多路抓图相机 |
| --- | --- | --- |
| robot-core 控制设备（`deviceId`） | `camera` | 不参与（不可控制） |
| sensor-worker 采集 provider | `camera` | `cam01`…`cam08` |
| 相机编号（`cameraId`） | `devices.camera.camera_id`（示例 `VAR-1`，缺省 `cam01`） | `devices.cameras.items[].id` |

- `GET /api/v1/cameras` 返回 `{source, cameras:[{id, deviceId, kind: ptz|inspection, label, enabled, host}]}`，`source` 为 `settings` 表示没有读平台 YAML（开发态），此时只有球形一条；
- 配置加载期校验编号**非空、唯一、字符集合规**（`[A-Za-z0-9._-]{1,64}`）。重复编号会让 sensor-worker 的 provider 字典静默覆盖成一路，因此直接以 `ConfigurationError` 启动失败；
- `POST /api/v1/media/captures` 的 `deviceId` 语义为相机编号：可传 `camera`（球形设备名，兼容旧调用）、球形编号 `VAR-1`、多路编号 `cam01`…；服务端统一映射到 provider key。未知或已禁用的编号返回中文可读错误；
- 巡检 / 视频流启动时按目录校验 `cameraIds`，未配置的编号、`mode=eight` 但已启用相机不足 8 路都会在 API 层给出中文提示（未配置平台 YAML 时不做此校验，交给 inspection-worker 兜底）。
