# 本地运行说明（开发阶段）

当前只在项目目录内启动/停止进程，**不写 `/etc`、不使用 systemd、不改系统包**。
工控机上的服务托管方式等进入部署阶段再定。

目录布局（Linux；项目可放任意目录、取任意名字，下文以 `<项目>` 代指）：

```text
<项目>/                            # backend/ 内容整体
  robot.env                       # 凭据与环境变量（cp robot.env.example robot.env；已在 .gitignore 中，不进仓库）
  .venv/                          # uv sync 生成（开发机复制来的会自动重建）
  app/                            # FastAPI 后端 + robot_core 运行时 + cloud_agent
  configs/hardware.yaml           # 硬件与数据库配置
  configs/cloud-agent.toml        # 云端接入配置（首次在页面上保存时生成）
  data/  run/  logs/              # 运行数据 / UDS socket / 日志（运行时生成）
```

## 前置准备

```bash
# 1) 安装 uv（Python 与依赖管理器；已装可跳过）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2) PostgreSQL：创建业务库与账号（对应 configs/hardware.yaml 的 DSN）
sudo -u postgres psql -c "CREATE ROLE robot LOGIN PASSWORD '<ROBOT_DB_PASSWORD>';"
sudo -u postgres psql -c "CREATE DATABASE robot_platform OWNER robot;"

# 3) 凭据：项目根执行
cp robot.env.example robot.env && chmod 600 robot.env
```

`.venv` 若不是在本机 Linux 创建（例如从其他平台复制），`uv sync` 会自动重建；
若提示环境不兼容，先 `rm -rf .venv` 再同步。运行期不依赖 uv。

## 启动、停止、检查

```bash
chmod +x deploy/quick_start_up.sh

deploy/quick_start_up.sh check     # 冒烟检查：配置 + PostgreSQL 连通（不驻留进程）
deploy/quick_start_up.sh start     # 启动全部：robot-core 先行，其余随后
deploy/quick_start_up.sh status    # 各组件状态 + healthz（含 cloud_agent）
deploy/quick_start_up.sh stop      # 全部停止（与启动相反的顺序）
deploy/quick_start_up.sh restart
```

可用环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `ROBOT_ENV_FILE` | `<项目>/robot.env` | 凭据文件 |
| `ROBOT_CONFIG` | `configs/hardware.yaml` | 进程配置 |
| `BACKEND_PORT` | `8100` | 后端端口 |
| `ROBOT_CLOUD_AGENT_ENABLED` | `true` | `false` 时不启动 cloud-agent |
| `ROBOT_CLOUD_AGENT_CONFIG` | `configs/cloud-agent.toml` | cloud-agent 配置 |

日志在 `logs/<组件>.log`，pid 在 `run/<组件>.pid`。脚本只处理自己托管的进程；
不在 pidfile 里的实例默认不动，`stop --all` 才会一并停止。

启动顺序：robot-core（等待 IPC health）→ 感知进程（等待两个 socket）→ 可选 sync-agent
→ cloud-agent → backend（等待 `healthz` 的 `coreStatus=ok`）。
重复绑定活跃 socket 会报错，无法替换正在工作的实例。

## cloud-agent（云端接入）

已并入 `app/cloud_agent`，作为**后台服务进程**运行——和 `inspection-worker`、`robot-core`
同一套管理方式：独立 pid、独立日志、单独启停、常驻事件循环；只是共用 backend 的 `.venv`。

```bash
.venv/bin/python -m app.cloud_agent
```

- 只出站连云端，不监听 TCP；本地控制面默认 `run/cloud-agent.sock`
- backend 的「云端接入」页面经 `ROBOT_CLOUD_AGENT_SOCKET`（默认同上）访问它
- 未启动时页面显示「未运行」，本地业务不受影响
- 凭据默认放 `<项目>/.cloud-agent/`（0600），可用 `ROBOT_CLOUD_AGENT_STATE_DIR` 改

## 联调与开发（无硬件）

```bash
uv sync                       # 一次装齐全部依赖（含 cloud-agent 用的 websockets/httpx）
uv run fastapi dev            # http://127.0.0.1:8000（需先启动控制与感知进程）
uv run pytest                 # 全量测试（需要 PostgreSQL 测试库；纯单元用例自动跳过）
```

`configs/dev.yaml` 连接本机 PostgreSQL（密码经 `ROBOT_DB_PASSWORD` 注入），
可运行控制、感知与后端三个进程：

```bash
uv run python -m app.robot_core.processes.robot_core --config configs/dev.yaml --check
```

## 数据库迁移与权限

版本由 `app/migrations` 统一管理，支持空库与旧 v1–v7 数据库升级，原业务数据保留。
先备份生产库，再使用数据库所有者账号执行迁移；服务进程使用独立、非所有者、非超级用户账号。
`robot.env.example` 默认关闭自动迁移，首次启动前必须执行：

```bash
set -a; . ./robot.env; set +a
# ROBOT_MIGRATION_DATABASE_URL 指向所有者账号；未配置时沿用 YAML storage.database_url。
.venv/bin/python -m app.robot_core.storage.migrate --config configs/hardware.yaml upgrade
.venv/bin/python -m app.robot_core.storage.migrate --config configs/hardware.yaml check
```

`ROBOT_AUTO_MIGRATE=false` 时，API 和感知进程仅校验版本；控制进程允许数据库离线启动，
急停先执行、审计后补。`check` / 进程 `--check` 只读检查数据库，不升级结构。
后续升级仍由所有者执行 `upgrade`；迁移带事务锁，重复执行安全，降级需从备份恢复。

分别创建新的 LOGIN 角色（密码由部署管理），再由所有者授予各进程所需表与结果视图权限：

```bash
.venv/bin/python -m app.migrations.permissions \
  --backend-role robot_api --core-role robot_control \
  --perception-role robot_perception --sync-role robot_sync
```

授权命令优先使用 `ROBOT_MIGRATION_DATABASE_URL`。它只授予权限，不会撤销角色已有权限；
请使用新角色，勿让服务账号继承所有者权限。后端设置 `ROBOT_DATABASE_URL`；运行时分别设置
`ROBOT_CORE_DATABASE_URL`、`ROBOT_PERCEPTION_DATABASE_URL`、`ROBOT_SYNC_DATABASE_URL`。
这几个地址应指向同一数据库，使用不同角色；未设置运行时地址时继续使用 YAML 原地址。

启动时会检查 YAML 与环境变量中的机器人身份、运行模式和数据库目标是否一致，冲突则停止启动；
检查不连接数据库，也不会在错误信息中输出密码。数据库账号和密码可以不同，目标主机、端口、库名须一致。
`localhost`、`127.0.0.1`、`::1` 按本机目标比较，Unix socket 地址通过 DSN 的 `host` 查询参数比较。
数据库目标须显式提供主机（或 Unix socket 目录）和库名，不支持通过 libpq service 文件隐式指定目标。
快速启动脚本会把所选 YAML 传给后端；手动启动后端时设置 `ROBOT_PLATFORM_CONFIG`
可启用相同检查（包含后端从 `robot.env` 读取的实际配置）。

内部巡检请求由共享契约校验，非法字段统一返回 `INVALID_REQUEST`；REST、WebSocket 与 Base64 图片格式保持不变。
巡检仓储已使用 SQLAlchemy 编译命名参数，继续复用 psycopg 连接池，状态、事件、outbox 和通知仍处于同一事务；
其余旧仓储保留兼容层，可逐个迁移。架构分层静态检查纳入默认 pytest，阻止 API/服务直接依赖运行时实现，
以及运行时反向依赖后端业务层。

控制进程仅写控制与状态表；感知进程写媒体、推理、巡检表；后端仅写工作台与鉴权表、读结果视图。
事件与 outbox 随业务变更在同一事务提交。感知进程重启后，中断任务使用原 `failed` 状态返回，保留进度，
需要重新创建任务。SIGTERM 会关闭 socket、停止任务并释放相机与模型。

预览每相机最多保留 16 帧，启动时清理旧预览；历史任务证据继续保留，保持原图片接口可用，
需按业务保留期单独归档并监控磁盘容量。急停审计位于 `data/emergency-audit`，成功重放后删除。

## 鉴权

- 默认 `ROBOT_AUTH_ENABLED=false`（开放模式，与旧前端联调契约一致）；
- 开启后：`/api/v1/**`（除 `/auth/login|refresh|logout`）要求
  `Authorization: Bearer <accessToken>`；首次启动自动创建 `admin`
  （密码见 `ROBOT_DEFAULT_ADMIN_PASSWORD`，登录后请立即修改）；
- 令牌签发：`POST /api/v1/auth/login` → `accessToken` + `refreshToken`；
  刷新 `POST /api/v1/auth/refresh`；登出 `POST /api/v1/auth/logout`。
