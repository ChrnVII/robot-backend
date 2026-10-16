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

说明：开发机整体复制来的 `.venv/` 是 Windows 环境、机器人上不可用，`uv sync`
会自动重建；若提示环境不兼容，先 `rm -rf .venv` 再同步。运行期不依赖 uv
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

五个进程：`robot-core`（设备唯一写入者）→ `sensor-worker` / `inference-worker` /
`inspection-worker` / `sync-agent`，另有 `backend.service`（FastAPI，端口 8100）。
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

脚本内部顺序与手工等价：先 robot-core（等待 socket 就绪），再三个 worker 与
sync-agent，最后 uvicorn 后端。手工单进程启动形如
`nohup .venv/bin/python -m app.robot_core.processes.robot_core --config configs/hardware.yaml &`。

## 联调与开发（Windows / 无硬件）

```bash
uv sync                       # 全量依赖（含 torch/opencv/串口；默认组已配置）
uv run fastapi dev            # http://127.0.0.1:8000（默认 mock 网关，无需机器人进程）
uv run pytest                 # 全量测试（robot_core + 后端；需要 PostgreSQL 测试库）
```

`configs/dev.yaml` 连接本机 PostgreSQL（密码经 `ROBOT_DB_PASSWORD` 注入），
可完整跑通五个进程
（`uv run python -m app.robot_core.processes.robot_core --config configs/dev.yaml --check`）。

## 鉴权

- 默认 `ROBOT_AUTH_ENABLED=false`（开放模式，与旧前端联调契约一致）；
- 生产开启后：`/api/v1/**`（除 `/auth/login|refresh|logout`）要求
  `Authorization: Bearer <accessToken>`；首次启动自动创建 `admin`
  （密码见 `ROBOT_DEFAULT_ADMIN_PASSWORD`，登录后请立即修改）；
- 令牌签发：`POST /api/v1/auth/login` → `accessToken` + `refreshToken`；
  刷新 `POST /api/v1/auth/refresh`；登出 `POST /api/v1/auth/logout`。

## 放行限制（沿用旧平台约定）

以下条件未满足前禁止切换生产控制权：真实设备协议与 HIL 测试、受控模型包验收、
生产凭据与网络分区、急停/失联停车验收。本仓库默认 `mock` / `simulated` 不驱动真实硬件。
