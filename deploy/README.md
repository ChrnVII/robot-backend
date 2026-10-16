# 部署说明（纯 Python 技术栈）

目标布局（Linux 工控机；**项目可放任意目录、取任意名字**，下文以 `<项目>` 代指）：

```text
<项目>/                            # backend/ 内容整体
  robot.env                       # 凭据与后端环境变量（cp robot.env.example robot.env，勿提交）
  .venv/                          # uv sync 生成（开发机复制来的会自动重建）
  app/                            # FastAPI 后端 + robot_core 运行时
  configs/hardware.yaml           # 生产配置（PostgreSQL + 硬件设备）
  data/  run/  logs/              # 运行数据 / UDS socket / 日志（运行时生成）
```

## 前置准备（机器人上一次性）

```bash
# 1) 安装 uv（Python 与依赖管理器；已装可跳过）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2) PostgreSQL：创建业务库与账号（对应 configs/hardware.yaml 的 DSN；
#    若端口/主机不同，同步修改 hardware.yaml 的 storage.database_url）
sudo -u postgres psql -c "CREATE ROLE robot LOGIN PASSWORD '<ROBOT_DB_PASSWORD>';"
sudo -u postgres psql -c "CREATE DATABASE robot_platform OWNER robot;"

# 3) 代码就位：backend/ 整个目录放到任意位置（如 ~/robot、/srv/robot、/opt/robot）
# 4) 凭据：cd <项目> && cp robot.env.example robot.env，填写后 chmod 600 robot.env
```

说明：拷入的 `.venv/` 若不是在本机 Linux 上创建（例如从其他平台复制），机器人上不可用，
`uv sync` 会自动重建；若提示环境不兼容，先 `rm -rf .venv` 再同步。运行期不依赖 uv
（systemd 直接调用 `.venv/bin/python`）。

## systemd（推荐，生产）

```bash
# 0) 代码放任意目录（示例 ~/robot），并完成「前置准备」
cd ~/robot

# 1) 凭据
cp robot.env.example robot.env       # 填写密码/密钥（含 CAMERA_PASSWORD），chmod 600 robot.env

# 2) 安装并启动（一条命令，自动完成：uv sync 全量依赖、创建 data/run/logs、
#    按「当前目录 + 当前用户 + 端口」渲染 systemd 单元、装入 /etc/systemd/system 并启动）
deploy/quick_start_up.sh install

# 3) 检查
systemctl status inspection-robot.target
curl -s http://127.0.0.1:8100/healthz
.venv/bin/python -m app.robot_core.processes.robot_core --config configs/hardware.yaml --check
```

- 单元文件是**模板**（`deploy/*.service.in`），`install` 时按当前目录、当前用户
  （可用 `SERVICE_USER=` 覆盖）与端口渲染后装入系统；**项目挪位置后重跑一次
  `install` 即可**，代码更新同理。
- 模型权重不在 git（`*.pt` 已忽略）：git 方式部署需单独拷贝
  `models/hs_detect/weights/best.pt`（整体复制/rsync 会自动带上）。

默认三个进程：`robot-core`（控制）+ `inspection-worker`（采集、模型、巡检）+
`backend.service`（FastAPI，端口 8100）。感知进程同时提供原采集与巡检 socket，模型首次推理加载、任务间复用。
原 `sensor-worker` / `inference-worker` 入口供独立调试，不能与感知进程同时运行。
`sync-agent` 按 `ROBOT_SYNC_ENABLED`（未设置时读 YAML `sync.enabled`）单独启用。
`robot-core` 必须先于其他进程就绪；模型或同步不可用不得阻止停车与人工接管。

## 手动快速启动（不配 systemd，调试/验证用）

```bash
# 前置：<项目>/robot.env 已就绪（cp robot.env.example robot.env 并填写）
chmod +x deploy/quick_start_up.sh

deploy/quick_start_up.sh check     # 冒烟检查：配置 + PostgreSQL 连通（不驻留进程）
deploy/quick_start_up.sh start     # 启动全部：自动加载凭据、按需重建 venv、robot-core 先行
deploy/quick_start_up.sh status    # 各组件状态 + healthz
deploy/quick_start_up.sh stop      # 全部停止（与启动相反的顺序）

# 可用环境变量：
#   ROBOT_ENV_FILE  凭据文件（默认 <项目>/robot.env）
#   ROBOT_CONFIG    进程配置（默认 configs/hardware.yaml）
#   BACKEND_PORT    后端端口（默认 8100）
# 日志: logs/<组件>.log；pid: run/<组件>.pid
```

脚本内部顺序：检查迁移版本（开发自动迁移），先 robot-core（等待 socket 就绪），
再感知进程、可选 sync-agent，最后 uvicorn。升级前停止旧进程；脚本会停止旧 pid 文件记录的独立 worker，
systemd 安装会禁用旧独立 worker。重复绑定活跃 socket 会报错，无法替换正在工作的实例。
手工单进程启动形如
`nohup .venv/bin/python -m app.robot_core.processes.robot_core --config configs/hardware.yaml &`。

## 联调与开发（无硬件）

```bash
uv sync                       # 全量依赖（含 torch/opencv/串口；默认组已配置）
uv run fastapi dev            # http://127.0.0.1:8000（需先启动控制与感知进程）
uv run pytest                 # 全量测试（robot_core + 后端；需要 PostgreSQL 测试库）
```

`configs/dev.yaml` 连接本机 PostgreSQL（密码经 `ROBOT_DB_PASSWORD` 注入），
可运行控制、感知与后端三个进程
（`uv run python -m app.robot_core.processes.robot_core --config configs/dev.yaml --check`）。

## 数据库迁移与权限

版本由 `app/migrations` 统一管理，支持空库与旧 v1–v7 数据库升级，原业务数据保留。
先备份生产库，再使用数据库所有者账号执行迁移；服务进程使用独立、非所有者、非超级用户账号。
`robot.env.example` 默认关闭生产自动迁移，首次启动前必须执行：

```bash
set -a; . ./robot.env; set +a
# ROBOT_MIGRATION_DATABASE_URL 指向所有者账号；未配置时沿用 YAML storage.database_url。
.venv/bin/python -m app.robot_core.storage.migrate --config configs/hardware.yaml upgrade
.venv/bin/python -m app.robot_core.storage.migrate --config configs/hardware.yaml check
```

生产设置 `ROBOT_AUTO_MIGRATE=false`，API 和感知进程仅校验版本；控制进程允许数据库离线启动，
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
快速启动脚本会把所选 YAML 传给后端，systemd 模板使用 `configs/hardware.yaml`；
手动启动后端时设置 `ROBOT_PLATFORM_CONFIG` 可启用相同检查（包含后端从 `robot.env` 读取的实际配置）。

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
- 生产开启后：`/api/v1/**`（除 `/auth/login|refresh|logout`）要求
  `Authorization: Bearer <accessToken>`；首次启动自动创建 `admin`
  （密码见 `ROBOT_DEFAULT_ADMIN_PASSWORD`，登录后请立即修改）；
- 令牌签发：`POST /api/v1/auth/login` → `accessToken` + `refreshToken`；
  刷新 `POST /api/v1/auth/refresh`；登出 `POST /api/v1/auth/logout`。

## 放行限制（沿用旧平台约定）

以下条件未满足前禁止切换生产控制权：真实设备协议与 HIL 测试、受控模型包验收、
生产凭据与网络分区、急停/失联停车验收。本仓库默认 `simulated` 不驱动真实硬件。
