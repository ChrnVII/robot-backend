# 部署说明（纯 Python 技术栈）

目标布局（Linux 工控机）：

```text
/opt/inspection-robot/            # 本仓库（backend/ 内容）
  .venv/                          # uv sync 生成
  app/                            # FastAPI 后端 + robot_core 运行时
  configs/hardware.yaml           # 生产配置（PostgreSQL + 硬件设备）
  data/                           # 媒体 / 巡检输出 / SQLite（开发）
  run/                            # Unix Domain Socket（0660）
/etc/inspection-robot/robot.env   # 凭据与后端环境变量（见 .env.example）
```

## systemd（单机工控）

```bash
# 1) 同步代码（在开发机 backend/ 目录执行；排除开发环境与运行产物）
#   rsync -av --delete \
#     --exclude '.git' --exclude '.venv' --exclude '.pytest_cache' \
#     --exclude 'data' --exclude 'run' --exclude '__pycache__' --exclude '.env' \
#     ./ robot@<目标机>:/opt/inspection-robot/
# 也可在目标机直接 git clone；模型权重不在 git（*.pt 已忽略），需另行
# 拷贝 models/hs_detect/weights/best.pt（rsync 方式会自动带上 models/）
sudo mkdir -p /opt/inspection-robot && sudo chown robot:robot /opt/inspection-robot

# 2) 安装依赖并准备运行目录
cd /opt/inspection-robot
sudo -u robot uv sync --frozen                       # 硬件/媒体/推理主机追加：
#   uv sync --frozen --extra hardware --extra media --extra inference
sudo install -d -o robot -g robot data run           # systemd ReadWritePaths 要求目录存在

# 3) 配置与凭据
sudo mkdir -p /etc/inspection-robot
sudo cp .env.example /etc/inspection-robot/robot.env  # 修改密码/密钥（含 CAMERA_PASSWORD，chmod 600）

# 4) 安装单元并启动
sudo cp deploy/*.service deploy/*.target /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now inspection-robot.target

# 5) 检查
systemctl status inspection-robot.target
curl -s http://127.0.0.1:8100/healthz
./.venv/bin/python -m app.robot_core.processes.robot_core --config configs/hardware.yaml --check
```

五个进程：`robot-core`（设备唯一写入者）→ `sensor-worker` / `inference-worker` /
`inspection-worker` / `sync-agent`，另有 `backend.service`（FastAPI，端口 8100）。
`robot-core` 必须先于其他进程就绪；模型或同步不可用不得阻止停车与人工接管。

## 联调与开发（Windows / 无硬件）

```bash
uv sync                       # 仅基础依赖
uv run fastapi dev            # http://127.0.0.1:8000（默认 mock 网关，无需机器人进程）
uv run pytest                 # 全量测试（robot_core + 后端）
```

`configs/dev.yaml` 使用 SQLite，可在开发机完整跑通五个进程
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
