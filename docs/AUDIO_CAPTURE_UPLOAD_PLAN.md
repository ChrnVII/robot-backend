# 声音采集优化与音频上云方案（鸡群异常声检测）

> **基线工程**：`E:/xunjian/robot-backend`（FastAPI 后端 + robot_core 五进程，纯 Python 单体）+ `E:/xunjian/robot-frontend`
> **部署形态**：**裸机 systemd（已确定，不使用镜像）**
> 状态：方案定稿待实现 · 复核日期 2026-10-16
> 本版取代旧基线版本（`E:/Robot Project` + `inspection-robot`：Java 后端 + Docker 部署）

---

## 0.0 实测定稿（2026-10-19，真机数据，与本文冲突处以本节为准）

一期采集（P0）已在真机完成参数整定并**落地实现**，以下数据全部来自 `mjl` 机器人实测：

### 硬件与系统

| 项 | 实测值 |
|---|---|
| 麦克风 | Audio-Technica ATR USB microphone，**card 1 / device 0**（`/dev/snd/pcmC1D0c`），USB full speed（UAC 1.0） |
| 板载声卡 | card 0 = HDA Intel PCH / ALC897（**不是录音目标**，但 ALSA `default` 会落到它） |
| 硬件原生格式 | `RATE: [44100 48000]`、`CHANNELS: 2`、S16_LE —— 44.1 kHz 原生支持；单声道由 ALSA `plug` 层下混，无需 asound.conf |
| PortAudio | `sounddevice 0.5.6` 可用（需系统 `libportaudio2`，PyPI wheel 不带，见 §5.2） |
| 音频服务栈 | **PulseAudio 已 mask**（方案 B，机器人当哑终端）；`inspection-worker` 服务直连 ALSA |
| 麦克风回环 | `numid=3` Mic Playback Switch 已关（旧程序 0 dBFS 削波的元凶，疑似监听正反馈） |

### 冻结的采集参数（已写入三份 YAML + schema + `AudioSettings` 默认值）

| 参数 | 值 | 依据 |
|---|---|---|
| `device` | **`"USB Audio"`** | **按 PortAudio 设备名匹配**，不写索引（索引随 USB 枚举漂移）。实现上先做本地名单子串匹配，匹配不到把字符串原样交给 PortAudio/ALSA 解析（实测 `"USB Audio"` 可命中 `ATR USB microphone: Audio (hw:1,0)`），**绝不静默回退默认设备** |
| `sample_rate` | **44100** | 硬件原生；旧生产程序 `mjlaudio.py` 同值 |
| `channels` | **1** | ALSA plug 自动 2→1 下混；旧生产程序同值 |
| `duration_seconds` | **10.0** | 方案 §3.10 |
| 采集增益 | **`amixer -c 1 cset numid=8 18`**（约 +16 dB，范围 -2~+20 dB） | 实测扫档：gain 12/15/18 时 max_volume 分别为 **-30.3 / -25.7 / -20.5 dB**，18 落在 -30~-6 dB 目标区间；已 `alsactl store`，并在服务单元 `ExecStartPre` 兜底固化（USB 卡开机枚举晚，alsa-restore 可能抢不到） |
| 电平健康区间 | max -30 ~ -6 dBFS | 空场实测 -39 dBFS（增益 6）偏低，+16 dB 后正常 |

### 已实现（一期采集 + 回放，2026-10-19）

| 项 | 状态 |
|---|---|
| `sensing/audio.py` 流式采集（InputStream 分块、可中断、超时、重试、RMS/峰值/削波检测） | ✅ 已实现 |
| 质量判定 `quality_state`（valid / degraded / invalid）写入 `media_assets` | ✅ 已实现 |
| 模拟模式产合法静音 WAV（替代 `simulated:...` 文本字节） | ✅ 已实现 |
| `AudioCaptureError` → IPC `CAPTURE_FAILED` → 后端 409 | ✅ 已实现 |
| 只读接口 `GET /media/assets`（列表/分页）、`GET /media/assets/{id}`、`GET /media/assets/{id}/content`（含 Range，音频拖动播放） | ✅ 已实现 |
| 前端：10 秒录音按钮、录音记录面板、内嵌试听（blob 播放带登录态）、下载 | ✅ 已实现 |
| 部署：`SupplementaryGroups=audio`、`ExecStartPre` 固化回环关+增益、`quick_start_up.sh` 装 libportaudio2 + 设备预检 | ✅ 已实现 |

### 本文过期章节更正（照做会踩坑）

1. **§11.2 迁移路径失效**：`storage/schema.sql` 与 `database.py` 的 `_upgrade_existing_tables()` 已不存在；现走 Alembic（`app/migrations/versions/`，当前最新 `0002_perception.py`）。
2. **§5.1 单元对象更正**：默认部署跑的是 **`inspection-worker.service`**（它同时监听 sensor socket），`SupplementaryGroups=audio` 与 amixer 固化应加在该单元（本次两个单元都加了）。
3. **§0.1 / §5.5 依赖管理更正**：本仓库 `pyproject.toml` 无 `[dependency-groups]`，`sounddevice` 已在主 `dependencies`；`uv sync --group inference` 等命令不适用。
4. **旧程序 `project/` 目录结论**（2026-10-19 勘察）：旧生产录音 44.1 kHz/单声道/3 s、不指定设备索引、音频只落本地不上传——与本方案一期一致；`project/` 仅作参考，不再改动。

---

## 0. 基线复核结论（先读这一节）

方案是在旧工程（Java 后端 + Docker 运行时）上做的。新工程结构变化很大，下面逐条说明**哪些结论作废、哪些仍然成立、复核中新发现什么问题**。

### 0.1 基线差异

| 维度 | 旧基线 | **新基线（robot-backend）** | 对方案的影响 |
|---|---|---|---|
| 后端 | Java Spring Boot（`backend/`） | **Python FastAPI**（`app/`，端口 8100） | 后端侧改造点全部改为 Python（`app/schemas`、`app/services`） |
| 运行时位置 | `runtime/src/inspection_robot/` | **`app/robot_core/`** | 全文代码路径引用改锚 |
| IPC | `inspection_robot/ipc` | **`app/ipc`**（后端与运行时共用） | 新事件/新进程照此引用 |
| `robot-api` 进程 | 独立进程 | **已删除**，对外接口由 FastAPI 承担 | 无需再考虑该进程 |
| 部署 | `Dockerfile` + `docker-compose.yml` | **`deploy/*.service.in` 模板 + `deploy/quick_start_up.sh`** | 所有落点改到 systemd 模板 |
| 服务用户 | 3 个（`robot`/`robot-sensor`/`robot-inference`） | **单一 `User=__SERVICE_USER__`** | 跨用户 socket 权限问题消失 |
| 配置文件位置 | `/etc/inspection-robot/hardware.yaml` | **`<项目>/configs/hardware.yaml`** | 相对路径不再错位（见 0.2） |
| 密钥注入 | 未实现（待办） | **`robot.env` + `EnvironmentFile=`** | 已落地，但有新问题（见 0.4） |
| 依赖管理 | pip / venv | **uv + `[dependency-groups]`** | 新增依赖写 `pyproject.toml` 的组 |
| 模型资产 | `model_library/` + `catalog.json` + `models/` | **仅 `models/hs_detect/weights/best.pt`，且不在 git** | ONNX 约定证据消失（见 0.2）；`package_root` 失效（见 0.4） |
| 前端 | `frontend/src` | **`robot-frontend/src`** | 引用改锚 |

### 0.2 已作废的旧结论（**不要再执行**）

1. **「裸机部署必须把路径全改成绝对路径」——作废。**
   旧基线把配置放在 `/etc/inspection-robot/`，导致 `../data/media` 解析到 `/etc/data/media`。新工程配置在 `<项目>/configs/hardware.yaml`，`../data/media` = `<项目>/data/media`，而单元里 `ReadWritePaths=__PROJECT_DIR__/data __PROJECT_DIR__/run` 正好覆盖。**路径正确，无需绝对路径改造。**
2. **「socket 目录跨用户权限需 tmpfiles.d + UMask」——作废。**
   新工程六个单元统一 `User=__SERVICE_USER__` / `Group=__SERVICE_USER__`，同一用户创建与连接，不存在跨用户授权问题。
3. **「`sensor-worker` 补 `/dev/snd` 透传」——不适用。**
   不再使用镜像，`docker-compose.yml` 不参与部署。改为系统组权限（见 §5.1）。
4. **「ONNX 是项目既定约定」——原证据失效，但结论已重新确认。**
   新工程**没有 `model_library/` 目录、没有 `catalog.json`**，全仓（`.py/.yaml/.toml/.md`）**`onnx` 出现 0 次**，所以旧依据不成立。**经确认，音频模型仍走 ONNX**——但这是**新决策**，理由见 §6.2（不再是"项目已约定"）。
5. **「每个 worker 都必须拿到全部密钥」——风险降级但不归零。**
   新工程用单一 `robot.env` + 每个单元 `EnvironmentFile=`，结构上已解决"每个 unit 单独配"的问题。但 `app/robot_core/common/config.py:19-35` 的 `_expand_environment` 仍是**整棵 YAML 树一次性展开**（未被改动），所以 `robot.env` 一旦缺 `ROBOT_DB_PASSWORD` 或 `CAMERA_PASSWORD`，**六个单元会全部启动失败**。`quick_start_up.sh:51-56` 会预检这两个变量，但 systemd 直接启用时不预检。

### 0.3 复核确认：仍然成立（代码层面逐字未改）

| 结论 | 证据 |
|---|---|
| 音频采集仍是**阻塞式一次性** `sd.rec()` + `sd.wait()` | `app/robot_core/sensing/audio.py` 与旧版逐字相同（仅 import 改为 `app.robot_core`） |
| 默认参数仍是 **16 kHz / 单声道 / 3 秒** | 同上（`AudioSettings` 默认值），且三份配置一致 |
| 模拟模式**仍用 `simulated:...` 文本字节冒充 WAV** | `app/robot_core/processes/sensor_worker.py:24` |
| 媒体落盘仍是 **`file://` URI、无时长/采样率元数据** | `app/robot_core/storage/media.py` 逐字未改（仅 sha256/size/mime/quality_state） |
| outbox **仍只传 JSON，无二进制通道** | `app/robot_core/sync/transport.py:61` `Content-Type: application/json` |
| **`enqueue_outbox()` 仍无任何生产调用方**（只有单测） | 全仓 grep 命中仅测试 |
| `sync.enabled: false`（三份配置一致） | `configs/{base,dev,hardware}.yaml` |
| `models` 装配层仍是**单模型硬编码** | `app/robot_core/inference/registry.py:44-61`：只注册 `heat_stress`，`elif allow_test_adapter else raise` |
| `MediaCaptureRequest` **无 parameters 范围校验** | `app/schemas/media.py:12-17` |
| **无音频证据接口** | 只有 `GET /inspections/{taskRunId}/images/{imageId}`（`app/api/endpoints/inspection.py:75`） |
| schema 版次仍是 **5** | `app/robot_core/storage/database.py:58` |
| 补列唯一路径仍是 `_upgrade_existing_tables` | `app/robot_core/storage/database.py:63` 的 `additions` 字典 |
| 前端仍用 16 kHz / 3 秒 | `robot-frontend/src/views/DetectionsView.vue:58` |

**结论：方案的 P0（采集内核）、P2（边缘推理）、P3（归档外发）主体全部不变**，只是代码路径和落点文件要改锚。

### 0.4 复核新发现的问题（**必须加进本次改造**）

| # | 问题 | 位置 | 严重度 |
|---|---|---|---|
| a | **`robot.env` 含真实凭据，却被 git 跟踪** | 已被 `git ls-files` 确认；`.gitignore` 只忽略了 `.env`，漏了 `robot.env` | **高（安全）** |
| b | `models.package_root: ../model_library/packages` 指向**不存在的目录** | 三份配置都有；新工程没有 `model_library/` | 中 |
| c | 模型权重 **不在 git**，首次部署必须单独拷贝 | `models/hs_detect/weights/best.pt`（`*.pt` 被忽略） | 高（部署） |
| d | `inference-worker` 单元注释写 `uv sync --extra inference`，命令是错的 | `deploy/inference-worker.service.in:14`；`pyproject.toml` 用的是 `[dependency-groups]`，正确为 `uv sync --group inference` | 低（文档） |
| e | 六个单元**都没有 `After=postgresql.service`** | `robot-core` 只 `After=local-fs.target systemd-udev-settle.service` | 中（开机竞态） |
| f | **无 `SupplementaryGroups=audio`** | `deploy/sensor-worker.service.in` | **高（录音前提）** |
| g | `quick_start_up.sh` 只预检 venv 与两个环境变量，**不检查系统库** | `ensure_venv()` / `load_env()` | 中 |
| h | 单一服务用户 = 单一权限域，`robot.env` 全部凭据同域 | 六个单元共享 `EnvironmentFile` | 中（安全设计） |
| i | `ReadWritePaths` 指向 `<项目>/data`、`<项目>/run`，目录由 `install` 创建 | `quick_start_up.sh:152` 有 `mkdir -p` | 低（需实测缺失时的行为） |

---

## 1. 决策摘要

| 决策点 | 选定方案 |
|---|---|
| 音频用途 | **鸡群异常声检测**（异常鸣叫 / 咳嗽 / 呼吸道音）+ 留证 + **数据集回流**；设备声（风机 / 料链）作为噪声排除 |
| 检测位置 | **机器人本体（边缘推理）**，由 `inference-worker` 加载模型在线完成 |
| **实施分期** | **一期（当前）：只做采集 + 数据集回流**（模型未训练，先用真实数据训练）；**二期：加入本体推理** |
| **上传节奏** | **任务级归档**（一次巡检任务一个归档包）+ **每日一次批量上传**（不实时） |
| 上传通道 | **对象存储预签名直传**（S3 / MinIO / 腾讯云 COS），机器人只发元数据事件 |
| 音频编码 | 采集 **WAV 44.1 kHz / 单声道 / 16-bit**（无损母版，不转码）；归档可选转 FLAC（§6.1） |
| **模型交付格式** | **ONNX（已确认）** —— 训练侧导出 ONNX，见 §6.2 |
| **云端位置** | **外部云平台（另仓实现）** —— 本方案只定义机器人侧契约（§13） |
| **部署形态** | **裸机 systemd（已确定）**，落点见 §5 |
| 采集增强 | **流式分块采集 + 实时电平 + 超时/失败重试**（配套质量检测） |
| 本地保留 | 上传成功后本地保留 N 天再清理 |

设计原则沿用项目既定约束：媒体二进制走对象存储、不写业务库；IPC 不传 Base64；事件走 outbox/inbox 至少一次投递 + 幂等；`robot-core` 是业务数据库唯一写入者；`api/services` 不得直接 import `robot_core` 内部实现（`readme.md:53-54`）。

**两条路径解耦**（核心设计点，见 §3.11）：

| 路径 | 数据 | 去向 | 保真要求 |
|---|---|---|---|
| **档案路径** | 44.1 kHz 无损母版 | `<项目>/data/media` → 归档 → 对象存储 → 训练集 | **不降采样、不做 AGC/降噪** |
| **推理路径**（二期） | 模型输入张量 | `inference-worker` 内存 | 由 adapter 从母版**重采样**得到，率可 ≠ 采集率 |

---

## 2. 目标数据流

### 2.1 一期（当前阶段：采集 → 训练数据集）

```text
前端 录音按钮（robot-frontend DetectionsView）
  → FastAPI  POST /api/v1/media/captures   （app/api/endpoints/media.py）
  → MediaService（app/services/media.py）校验 deviceId↔mediaType
  → 网关 → UDS → sensor-worker
  → SoundDeviceAudioProvider 流式采集 44.1 kHz/10 s（P0 改造后）
  → MediaStore 原子落盘 <项目>/data/media/audio/<asset_id>.wav
  → robot-core  asset.register（同事务入队 media.asset.created）
  → Archiver（巡检任务结束）打包 <项目>/data/archives/<task_run_id>/
  → outbox（next_retry_at = 次日 02:00）
  → sync-agent  ← 每日窗口
  → 预签名直传对象存储 → 云端入库 → 训练集
```

### 2.2 二期（模型就绪后叠加本体推理）

```text
采集落盘（同上）
  → 触发一次 inference 请求（编排侧）
  → inference-worker: AudioAnomalyAdapter（ONNX）
       母版 44.1 kHz → 确定性重采样 → 模型输入率 → 输出事件
  → robot-core 写 audio_events（新增表）
  → 异常命中 → alerts（复用现有表，evidence_asset_id 指向音频资产）
  → 高优先级立即上传异常片段；正常基线样本随每日归档走
```

---

## 3. 面向鸡群异常声检测的采集规范

任务性质是**强噪声下的生物声事件检测**：目标信号是鸡的异常鸣叫/咳嗽（短时、高频、非周期），设备声（风机、料链）是**结构化、周期性、可预测**的噪声。两者频带重叠，必须靠带宽与周期性在**分析端**分离。

### 3.1 采样率：44.1 kHz（**核心决策**）

唯一决定性的理由是**采样率不可逆**：44.1 kHz 可随时下采样到 16 kHz 训练；16 kHz 采到的数据**永远回不到 8 kHz 以上**。鸡的咳嗽/啰音高频成分可达 8–12 kHz，一旦窄带采集，等于把信号与噪声**一起切掉**，后续无法分离。

原则：**采集端取上限，训练端按需下采样。** 体积代价可忽略（10 秒 882 KB）。若麦克风硬件原生 48 kHz，用 48 kHz 同样成立。

### 3.2 编码：采集期无损（WAV），禁止有损

禁止 Opus / AAC / MP3：低码率有损编码会做**低通滤波**，恰好抹掉异常所在的高频特征；编码伪影还可能被模型误判为异常。WAV 用标准库 `wave` 即可，零依赖（已在 `sensing/audio.py` 实现）。

### 3.3 位深：16-bit

动态范围 ~96 dB，已远高于 MEMS 麦克风能力，24-bit 无收益。16 bit 的余量正好同时容纳"弱鸡叫"与"强设备噪声"。

### 3.4 采集端只保留"脏"母版：不做 AGC，也不做降噪

- **禁止任何 AGC / 自动增益**：异常检测对电平与频谱的可复现性极敏感，自动增益会让同一声音录出不同电平，直接污染特征分布。
- **也不要在采集端做降噪/带通/高通**：抑制设备噪声的策略会随模型迭代而变，采集时切掉的频段不可恢复。
- 硬件若有 AGC/降噪，**在采集配置里关掉**；把实际增益写入 `metrics`。
- **干净版本在分析端按需生产**（谱减、自适应滤波、针对风机基频的梳状滤波），可复现、可迭代、可复算。

### 3.5 采集时长：10 秒

3 秒偏短，异常往往偶发，短窗口容易整体漏掉。取 10 秒（DCASE 惯例也是 10 秒片段）。

### 3.6 增益整定：为微弱鸡叫抬电平，但给设备噪声留足余量

本场景最实际的矛盾是"鸡叫弱、设备噪声强"。做法：**固定增益 + 一次性现场整定**——麦克风尽量靠近鸡笼、加防风防尘罩，把安静鸡叫的 SNR 做上去，但**给设备噪声峰值留足余量不削波**。削波检测必须开。

### 3.7 必须同步积累「正常」基线样本

异常检测靠对比正常分布。计划性巡检要**稳定产出正常样本**，不能只在怀疑异常时才录——否则数据集严重偏向异常，模型学不出"正常"。元数据必须齐全（§3.9），否则无法分层建模与偏差排查。

### 3.8 质量门禁

- 削波 → `quality_state = degraded`
- 全静音 / 有效时长达不到 → `invalid`
- `invalid` 样本可上传留证，但**不得进入训练集**（训练侧用 `quality_state` 过滤）

### 3.9 标注体系与元数据

标签集：`normal_call` / `abnormal_call` / `cough` / `noise_device` / `silence` / `other`。
**`noise_device` 要作为显式负类**，让模型学会主动排除设备噪声，而不是把它当异常。

每条记录需带：`coop_id / point / layer / cage_id / captured_at / device_id / gain_db`，并建议记录**设备运行状态**（风机开/关、料链运行/停止）——设备噪声与运行状态强相关，既能帮分析端分层降噪，也能解释误报来源。

### 3.10 参数一经确定即冻结（**一期就要执行**）

**采集参数 = 训练参数 = 推理参数。** 采样率、位深、声道、增益策略一旦定下，训练与推理必须一致，否则域不匹配显著降低检出率。数据正在采，中途改参数会让数据集前后不一致，只能靠元数据事后筛段，代价极高。**先定参数、写进配置、再批量采集。**

### 3.11 采集参数与模型输入率**解耦**

既然检测在本体、不等历史数据，采集率不必等于模型输入率：

- 采集端固定 44.1 kHz 无损母版（保真、可复算、可回溯）；
- 模型输入率由模型自己定（如 16 kHz），adapter 在推理时从母版**确定性重采样**。

于是约束从"采集参数 = 训练参数"降级为"**重采样链固定且版本化**"——以后换模型输入率**不再需要重采历史数据**。前提：重采样算法写进 `adapter_version`，模型率写进 `model-manifest.json`。

### 3.12 不存特征，只存波形

特征（mel/STFT）不可逆，参数一固化，改模型就要重算全部数据。存波形则任意重算，保留全部未来可能性。

---

## 4. P0 — 采集内核：流式 + 质量检测 + 重试

**改动文件**：`app/robot_core/sensing/audio.py`、`app/robot_core/sensing/service.py`、`app/robot_core/contracts/models.py`、配置三份、`tests/robot_core/unit/test_audio_provider.py`

### 4.1 现状问题（`sensing/audio.py`）

1. `sd.rec(frames...) + sd.wait()` 是**阻塞式一次性**采集：不能中途停止、无实时电平、无超时、无重试，失败直接抛 `AudioCaptureError`。
2. 无设备就绪探测，设备被占用/不存在时错误信息不明确。
3. 返回值是裸 `bytes`（WAV），**没有任何质量信息**通道。

### 4.2 改造要点

- 改用 `sd.InputStream(callback=...)` **分块采集**，逐块计算 **RMS / 峰值**，边采边判。
- **静音检测**：整段 RMS 低于阈值 → `invalid`；**削波检测**：达到满幅的样本占比超阈值 → `degraded`。
- 支持**中途停止**（`stop_event`）与**超时**（`duration + margin`）。
- **设备就绪探测 + 退避重试**（可重试错误重试，设备不存在等不可重试错误快速失败）。
- 质量结果写入 `quality_state`（`valid` / `degraded` / `invalid`），电平指标写入 `metrics`。

### 4.3 契约扩展

当前 `CaptureRequest`（`app/robot_core/contracts/models.py:95`）只有 5 个字段 + `parameters`。新增：

```python
@dataclass(frozen=True)
class CaptureResult:
    content: bytes
    quality_state: str = "valid"          # valid | degraded | invalid
    metrics: Mapping[str, Any] = field(default_factory=dict)  # rms/peak_db/clipped_ratio/duration
```

`CaptureProvider` 现为 `Callable[[CaptureRequest], bytes]`，要扩成可返回 `CaptureResult`，`SensorService`（`sensing/service.py`）做**归一化**以兼容现有摄像头 provider（返回裸 bytes 时按 `quality_state="valid"` 包装）。

### 4.4 `AudioSettings` 默认值与新增字段

| 参数 | 现值 | **目标值** | 允许范围 |
|---|---|---|---|
| `device` | `null` | `null`（系统默认输入设备） | 数字字符串自动转 int |
| `sample_rate` | 16000 | **44100** | 8000–192000 |
| `channels` | 1 | 1 | 1–8 |
| `duration_seconds` | 3.0 | **10.0** | >0 且 ≤60 |
| `gain_db`（新增） | — | 固定值，一次性整定 | 记录到 `metrics` |
| `silence_rms_threshold`（新增） | — | 待现场整定 | — |
| `clip_ratio_threshold`（新增） | — | 建议 0.001 | — |

三份配置（`configs/base.yaml:30`、`configs/dev.yaml:29`、`configs/hardware.yaml:92`）的 `devices.audio` 需**同步改**。`configs/schemas/base.schema.json` 的 `devices.audio` 目前只声明为 `"type": "object"`，无字段级约束，建议补齐。

---

## 5. P1 — 落点修正（裸机 robot-backend）

**前提**：部署形态确定为裸机 systemd，**不使用镜像**。以下全部落在 `deploy/*.service.in` 模板与 `quick_start_up.sh`，**不要直接改 `/etc/systemd/system/` 下的渲染结果**（重跑 `install` 会被覆盖）。

### 5.1 声卡访问（**录音的前提**）

在 `deploy/sensor-worker.service.in` 的 `[Service]` 段补：

```ini
SupplementaryGroups=audio
```

否则服务用户读不到 `/dev/snd/*`（宿主上通常是 `root:audio` + `crw-rw----`）。

替代方案：把服务用户加入 `audio` 组（`sudo usermod -aG audio <user>`）。两种都行，但**写进单元更稳**（不依赖人工操作的机器状态）。

验证：
```bash
sudo -u <服务用户> <项目>/.venv/bin/python -c "import sounddevice as sd; print(sd.query_devices())"
```
能列出输入设备即通过。

注意：单元里有 `ProtectSystem=strict` 但**没有 `PrivateDevices`**，所以 `/dev/snd` 本身是可见的，只差组权限。

### 5.2 系统库依赖（uv 管不到）

`uv sync` 只装 Python 包，**不会装系统库**。裸机必须手工补齐：

| 包 | 为什么必需 | 备注 |
|---|---|---|
| `libportaudio2` | **`sounddevice` 在 Linux 上运行时依赖系统 libportaudio** | PyPI wheel **不含** portaudio（与 Windows/macOS 不同）。Debian 系包名 `libportaudio2` |
| `libsndfile1` | 归档转 FLAC（§6.1）依赖它 | `soundfile` 的 Linux wheel 通常自带，但显式安装兜底 |
| （按需）`libglib2.0-0` | 部分发行版下 `opencv` wheel 间接依赖 | **`pyproject.toml` 用的是 `opencv-python-headless`，不需要 `libgl1`**——旧方案里"libgl1 必需"的结论不适用于新工程 |

**实测为准**（比查文档可靠）：
```bash
ldd <项目>/.venv/lib/python3.12/site-packages/cv2/cv2.abi3.so | grep "not found"
ldd <项目>/.venv/lib/python3.12/site-packages/_sounddevice*.so | grep "not found"
```

`quick_start_up.sh` 目前只检查 venv 与 `ROBOT_DB_PASSWORD`/`CAMERA_PASSWORD`（`ensure_venv()`、`load_env()`）。**建议在 `do_check` / `do_start` 前加一段系统库预检**，否则 `libportaudio` 缺失要到第一次录音才暴露。

### 5.3 模型权重不在 git（首次部署最容易漏的一步）

`configs/*.yaml` 指向 `<项目>/models/hs_detect/weights/best.pt`，但 `models/` 目录与 `*.pt` 都不在 git（`.gitignore` 的 `*.pt`）。`quick_start_up.sh install` **不会**帮你拉模型。

部署清单需要显式包含：把 `models/hs_detect/weights/best.pt` 拷到目标机器，并核对其 sha256 与 `configs/hardware.yaml:151` 一致。

**建议**：在 `quick_start_up.sh` 的 `do_check` 里加一步"模型权重存在性 + sha256 校验"，让缺失在冒烟检查阶段就失败，而不是等 `inference-worker` 启动即抛（`Restart=on-failure` 下会变成无声循环重启）。

### 5.4 失效配置：`models.package_root`

三份配置都有 `models.package_root: ../model_library/packages`，解析为 `<项目>/model_library/packages` —— **该目录在新工程中不存在**（`model_library/` 随旧工程一起消失了）。目前没有代码读取它（`registry.py` 走 `heat_stress.path`），属于残留。

**处理**：删除该项，或改为 `<项目>/models/packages` 以备二期使用。**注意 `configs/schemas/base.schema.json` 若对 `models` 子键有白名单约束，需同步**。

### 5.5 单元纠错与依赖顺序

1. **`deploy/inference-worker.service.in:14` 注释是错的**：写的是 `uv sync --extra inference`，而 `pyproject.toml` 用的是 `[dependency-groups]`（`default-groups = ["dev","hardware","media","inference"]`）。正确命令是 `uv sync --group inference` / `uv sync --no-group inference`。注释会误导部署人员。
2. **六个单元都缺 `After=postgresql.service`**。若 PostgreSQL 装在本机（既然不用镜像，PG 也不在容器里），开机时 `robot-core` 可能先于 PG 就绪 → 首启失败。`Restart=on-failure` 会重试，但会刷日志、延迟就绪。建议补 `After=postgresql.service` + `Wants=postgresql.service`。
3. **`sync-agent` 即使 `sync.enabled=false` 也会被 target 拉起**（`processes/sync_agent.py` 的 `handler` 返回 `state: disabled`，不启动 dispatcher）。符合预期，但要知道它会占一个常驻进程——**P3 打开 `sync.enabled` 后它才真正干活**。
4. **`ReadWritePaths` 的目录存在性**：`quick_start_up.sh:152` 在 `install` 时会 `mkdir -p logs data run`，所以走脚本没问题；但手工拷贝单元时若 `<项目>/data`、`<项目>/run` 不存在，需确认 systemd 是否会因 `ReadWritePaths` 指向不存在的路径而启动失败。**建议实测一次**；不放心可在单元里加 `ExecStartPre=/bin/mkdir -p`。

### 5.6 凭据管理（含一个**必须先修**的安全问题）

**⚠️ 立即处理**：`robot.env` **已被 git 跟踪**（`git ls-files` 确认），且内容**不是占位符**（`CHANGE_ME` 出现 0 次）——包含 `ROBOT_DB_PASSWORD`、`CAMERA_PASSWORD`、`ROBOT_JWT_SECRET`、`ROBOT_DEFAULT_ADMIN_PASSWORD` 的真实值。`.gitignore` 只忽略了 `.env`，**漏了 `robot.env`**。

处置顺序：
1. `.gitignore` 加 `robot.env`；
2. `git rm --cached robot.env` 并提交；
3. **轮换上述四个凭据**（本地历史里已存在，且文件可能已被复制分发）；
4. 若已推送过远端，需清理历史（当前 `git remote -v` 为空，尚未推送）。

其余凭据规范：
- `robot.env` 权限 `0600`，属主为服务用户（`deploy/README.md:26` 已要求，但**脚本未强制检查**，建议 `install` 时 `chmod 600` + 属主校验）。
- 单元用 `EnvironmentFile=-__PROJECT_DIR__/robot.env`，前缀 `-` 表示文件缺失不报错——**这是双刃剑**：文件丢了进程照样起来，然后在 `load_config` 阶段因缺变量崩溃。建议安装脚本强制要求文件存在（`do_install` 已检查，`do_start` 会 warn）。
- **单一权限域问题（可选改进）**：六个进程共享同一 `robot.env` 与同一服务用户，任一进程被攻破即拿到全部凭据（含摄像头密码与 JWT 密钥）。若要收敛，可用 `LoadCredential=` 按进程拆分凭据集合。当前阶段可接受，但**新增对象存储密钥后该风险会放大**，届时应重新评估。
- **`_expand_environment` 仍是整树 eager 展开**（`app/robot_core/common/config.py:19-35`）：任何 unit 缺任一必需变量都会整体启动失败。建议改为**惰性展开**（只展开被访问的 section），否则每新增一个凭据，都要确认六个单元都有。**注意：对象存储密钥、mTLS 证书路径都是增量。**

### 5.7 发布与回滚（无镜像形态）

不用镜像后失去"镜像即版本"的原子回滚。当前 `deploy/README.md:52-54` 的机制是：代码更新后重跑 `deploy/quick_start_up.sh install` 重新渲染并重装单元。

建议补强：
- 用 **git tag / commit 记录当前部署版本**，回滚 = `git checkout <tag>` + 重跑 `install`；
- 或在 `<项目>/` 旁建 `releases/<version>/` + `current` 软链，`__PROJECT_DIR__` 指向软链，回滚只需切软链（不触发重新渲染）。后者更干净，但要改 `install` 渲染逻辑。
- **好消息**：模型更新不再需要重建镜像，模型包机制（manifest + sha256）可以独立灰度下发（§7.4）。

---

## 6. 交付格式选型

### 6.1 A. 数据集格式（一期重点）

**模型还没训练，所以当前真正决定成败的是"采什么、怎么组织"，而不是模型格式。**

**格式：WAV 文件树 + `metadata.jsonl` + `archive-manifest.json`，不存特征。**

```text
<项目>/data/archives/<task_run_id>/
  manifest.json          # 机器可读：采集参数冻结值、资产清单、sha256、时间窗
  metadata.jsonl         # 每条资产一行：路径 + 元数据 + 事件/标签
  audio/<asset_id>.wav   # 或 .flac（见下）
```

要点：
- **用 JSONL 而不是 CSV**：事件/标签是嵌套列表（`events: [{label, start_s, end_s}]`），CSV 塞嵌套要转义成字符串，训练侧解析很痛苦。
- **绝不存 mel 特征**（§3.12）。
- **manifest 里记采集参数冻结值**（采样率 / 位深 / 声道 / 增益）——这是事后判断"这批数据是不是同一规格"的唯一依据。
- **train/val/test 按 `task_run_id` 或 `coop_id` 划分**，不要让同一段录音的相邻切片跨集，否则验证指标虚高。

**FLAC 归档（可选优化，但不再是"白拿"）**：
旧方案把 FLAC 当作"镜像里已装 `libsndfile1` 所以白拿"的优化——**这个前提在裸机形态下不成立**，`libsndfile1` 要自己装（§5.2）。装上之后：采集期仍存 WAV（实时、零依赖），归档期（离线批处理）转 FLAC，**无损**、省约 45% 流量，训练侧用 `soundfile` 读取完全无感。与"禁止有损编码"不矛盾。

**流量估算**（10 秒 882 KB，100 航点/任务，每天 3 次）：约 **265 MB/天/台，8 GB/月/台**。FLAC 后约 145 MB/天。若走 4G 计费流量，多台机器人下这个成本要评估。

### 6.2 B. 模型交付格式：**ONNX（已确认）**

**重要修正**：旧方案说"ONNX 是项目既定约定"，依据是 `model_library/README.md` 与 `catalog.json`。**这两个文件在新工程中都不存在，全仓 `onnx` 出现 0 次**，所以旧依据不成立。**经确认，音频模型走 ONNX**——以下是重新论证过的理由。

**现状**：`app/robot_core/inference/heat_stress.py` 用 `ultralytics.YOLO` 加载 `.pt`，走 torch。音频模型**不照抄这条路径**。

**选 ONNX 的理由（已按裸机形态重新论证）**：

1. **安全**（最强理由）：`torch.load` 走 pickle 反序列化，**远程下发模型等于远程执行代码**。对后续 OTA 下发音频模型是硬伤。ONNX 是纯算子图 + 数据结构。
2. **CPU 部署优化**：`configs/hardware.yaml:153` 是 `${MODEL_DEVICE:-cpu}`（注释写明默认 CPU 以便兼容无 NVIDIA 的机器）。ONNX Runtime 的 CPU EP 有图优化与量化支持，比 torch eager 更可控。
3. **推理开销更可预测**：二期要在本体跑 10 秒片段的音频模型，与巡检负载抢 CPU。ONNX Runtime 的线程数可显式约束（对应 §15 的算力预算风险）。

**注意：体积不再是主要理由**——因为热应激已经在用 torch/ultralytics，裸机上 torch 本来就要装，"ONNX 省 2 GB"的说法在新工程下不成立。诚实地按上面三条权重即可。

**热应激是否也迁 ONNX：建议分开决策、暂不动。** 热应激已验收（`configs/hardware.yaml:151` 的 sha256 已配置），迁移属额外风险；音频模型是新建的，直接走 ONNX 无历史包袱。

**因此 `pyproject.toml` 的 `inference` 组需加 `onnxruntime`**（与 `torch` / `ultralytics` 并存，因为热应激仍需要后者）。

**训练侧导出要求（写进模型交付规范）**：
- opset ≥17 固定；输入形状明确；
- **mel/STFT 前端要么放 Python 侧、要么作为图内固定子图，只选一种**（推荐前者，可单元测试）；
- 导出后必做 `onnx.checker` + **数值对齐测试（PyTorch vs ONNX 误差 < 1e-4）**——导出静默走样是这类项目最常见的事故；
- **先 fp32 上线**，int8 量化作为后续优化（量化可能损伤小能量瞬态判别力）；
- manifest metadata 写 `sample_rate` / `window_seconds` / `features` / `labels` / `training_dataset_version`。

---

## 7. P2（二期）— 边缘推理接入

**触发条件**：模型训练完成并导出。

### 7.1 现状：装配层是单模型硬编码

`app/robot_core/inference/registry.py`：
- `:42-45` `default_registry()` 只注册 `heat_stress`；
- `:48-61` `build_configured_adapters()` 只读 `models.heat_stress`，逻辑是 `if 启用… elif allow_test_adapter… else raise`。

后果：`InferenceRuntime` 本身支持多适配器，**运行时没问题、装配层没跟上**。如果只想在本体跑音频模型（关掉热应激），当前代码会直接 `RuntimeError: no verified production model package configured`（`:61`）——`inference-worker` 起不来，在 `Restart=on-failure` 下就是循环重启。**所以加音频模型必须重构这个函数为"按配置表遍历"。**

### 7.2 新增 `AudioAnomalyAdapter`

实现 `ModelAdapter` 接口（`inference/runtime.py`），注册为 `audio_anomaly`，capability 建议 `audio_anomaly_detection`：
- 从母版 WAV 读取 → **确定性重采样**到 manifest 声明的输入率（§3.11）→ 特征前端（Python 侧）→ ONNX 会话推理 → 输出 `{label, score, start_s, end_s}` 事件列表。
- 重采样算法与版本写进 `adapter_version`。

### 7.3 `build_configured_adapters()` 重构（**必须做**）

```python
# default_registry()：注册表 + 各模型 builder
# build_configured_adapters()：从"单模型 if/elif"改为"按配置表遍历"
for model_id, settings in models.items():
    if not isinstance(settings, Mapping) or not settings.get("enabled", False):
        continue
    ...  # 装配并写入 adapters[capability]
```

`allow_test_adapter` 逻辑保留为"无任何生产模型启用时"的兜底。

### 7.4 模型包发布

沿用现有 `ModelPackage`（`inference/package.py`，manifest + sha256 + `common/freeze.py`）。音频模型包路径建议 `<项目>/models/audio_anomaly/<version>/`。

### 7.5 编排者：谁来发 `InferenceRequest`

`realtime_demo.py` 有完整的"采集→推理→持久化"链路，但**只处理摄像头帧**（capability=`heat_stress_detection`）。音频没有对应物。

建议把音频编排放进 **`inspection-worker`**（保持 `robot-core` 非阻塞，符合其"设备唯一写入者 + 安全仲裁"定位）。二期可先做"巡检任务中按航点触发"，后续再演进为连续采集 + 触发切片。

### 7.6 幂等

沿用 `inference_runs` 的既有幂等键模式，建议 `audio_anomaly:{asset_id}:{model_sha256}`，避免同一资产重复推理入库。

### 7.7 数据模型：需要新表

`inference_detections`（`storage/schema.sql:195`）是 **bbox 导向**的：`camera_id` / `frame_id` / `x0y0x1y1` 全 NOT NULL，**装不了音频事件**。需新增：

```sql
CREATE TABLE IF NOT EXISTS audio_events (
  event_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL,
  task_run_id TEXT,
  label TEXT NOT NULL,
  score REAL,
  start_seconds REAL,
  end_seconds REAL,
  model_sha256 TEXT,
  adapter_version TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(asset_id) REFERENCES media_assets(asset_id)
);
```

而 `alerts`（`schema.sql:212`）**已有 `evidence_asset_id` + `source_run_id`**，**可以直接复用**，不用改表。

**建议一期就把 `audio_events` 建出来**（空表），避免二期再提一次 schema revision。

---

## 8. P3 — 任务级归档 + 事件外发（一期主线）

### 8.1 为什么不再逐条入队

上传节奏改为"一次巡检任务一个归档包 + 每日一次"，所以不再逐条资产触发，而是任务级打包。好处：**减少 outbox 事件数量、便于按任务追溯、天然对应"数据批次"概念**。

### 8.2 新组件 `Archiver`

建议位置：`app/robot_core/storage/archive.py`（纯存储层，无业务依赖）。

职责：任务结束时，把该 `task_run_id` 下的媒体资产打包到 `<项目>/data/archives/<task_run_id>/`，生成 `manifest.json` + `metadata.jsonl`（§6.1），可选转 FLAC，返回归档包路径 + sha256 + 大小。

### 8.3 入队：复用 `next_retry_at` 实现"每日上传"（**不需要写调度器**）

`outbox` 表已有 `next_retry_at` / `state` / `delivery_count` / `claim_token`（`storage/database.py:70-76` 的 additions），`OutboxDispatcher.drain_once()` 只取 `state=pending AND next_retry_at <= now` 的记录。

**所以：入队归档事件时把 `next_retry_at` 设为次日 02:00，归档就会自然等到窗口时间才投递。** 手动强制上传复用现有 IPC `drain_once` 即可。

### 8.4 入队点必须放在 `robot-core`（**这是当前最大断点**）

`PlatformStore.enqueue_outbox()` 在**整个 `app/` 里没有任何生产调用方**（只有单测在调），且 `sync.enabled=false`。**采集的音频目前永远不会离开机器人。**

建议：在 `robot-core` 处理 `asset.register` 的路径里**同事务**入队 `media.asset.created`，幂等键 `media.asset.created:{asset_id}`。

**为什么不能放 `sensor_worker`**：`processes/sensor_worker.py:24` 用的是 `MediaStore(root, None, robot_id)`（`store=None`，不写库），符合项目"`robot-core` 是业务数据库唯一写入者"的约束。保持这个边界。

### 8.5 注意事项

- `sensing/audio.py` / `media.py` 全程未改动，所以**落盘路径约定不变**：`<项目>/data/media/audio/<asset_id>.wav`。
- `media_assets.quality_state` 字段已存在，P0 的质量检测直接复用，**无需加列**。

---

## 9. P4 — 对象存储预签名直传（每日窗口）

### 9.1 新组件 `MediaUploader`

建议位置：`app/robot_core/sync/media_upload.py`。流程：
1. 向云端申请预签名 URL（复用现有 `sync` 的 mTLS 证书配置：`certificate_file` / `private_key_file` / `ca_file`）；
2. `PUT` 直传对象存储；**大文件走分片续传**（归档包可达上百 MB，单次 PUT 遇断网要能续传，否则每日窗口会被一次失败打乱节奏）；
3. 成功后回执 → 确认 outbox 条目标记为 `sent`。

### 9.2 `sync-agent` 扩展

`processes/sync_agent.py` 当前按"一条 outbox 记录 = 一次 HTTPS JSON POST"处理。需要按**事件类型分流**：
- `media.asset.created` / `archive.created` → 交给 `MediaUploader`（二进制路径）；
- 其他 → 走现有 `HttpsOutboxSender`（JSON 路径）。

**重试 / 租约 / 退避 / `max_attempts` 全部复用现有 outbox 机制**，不另造一套。

### 9.3 关于"优先级队列"（降级为二期）

原方案设想"异常片段走高优先级立即传"。既然一期不做本体推理，"异常"这件事机器人还不知道，**优先级机制降级到二期**（届时 `outbox` 加 `priority` 列）。

### 9.4 断网积压与容量规划

每日一次意味着**失败就是积压 24 小时**。需要：
- 容量规划（按 §6.1 的 265 MB/天 × 积压天数）；
- 积压可见性（`delivery_count` / `state` 已有，建议在 FastAPI 侧暴露一个只读积压接口）；
- 连续失败告警；
- **保留期 > 每日窗口 + 重试周期**（建议 ≥14 天，见 §10），且**归档包确认送达前不要删原始片段**。

---

## 10. P5 — 本地保留 N 天后清理

建议位置：`app/robot_core/storage/retention.py`。

规则：**仅清理「outbox 已 sent」且超期的资产**——顺序保护，绝不先删后传。

需要新增列（走 `_upgrade_existing_tables`，见 §11）：
- `media_assets.local_state`（`present` / `purged`）
- `media_assets.purged_at`

清理对象包括原始 WAV 片段与已确认送达的归档包。建议按"上传成功日 + N 天"计算，而不是按采集日。

---

## 11. 数据模型与迁移

### 11.1 表结构变化

| 表 | 变化 | 阶段 |
|---|---|---|
| `media_assets` | 新增 `local_state`、`purged_at`；`quality_state` 复用现有字段（写入真实质量值） | P0 / P5 |
| `audio_events` | **新建表**（§7.7） | 建议一期建空表 |
| `outbox` | 新增 `priority`（可选，二期） | 二期 |
| `alerts` | **不改**（`evidence_asset_id` + `source_run_id` 已够用） | — |
| `inference_detections` | **不用**（bbox 导向，见 §7.7） | — |

### 11.2 迁移落点（**两处都要改，缺一不可**）

1. **`app/robot_core/storage/schema.sql`**：新表 / 新列写进 `CREATE TABLE IF NOT EXISTS`。
2. **`app/robot_core/storage/database.py:63` 的 `_upgrade_existing_tables()`**：`CREATE TABLE IF NOT EXISTS` **不会给已存在的表加列**，所以新列必须加进 `:65` 的 `additions` 字典。这是**唯一的补列路径**。
3. **schema 版次 5 → 6**：`app/robot_core/storage/database.py:58` 硬编码 `VALUES (5, ?)`，另需同步检查 `app/robot_core/storage/migrate.py` 中的版次引用。

数据库现在**只支持 PostgreSQL**（`common/config.py:109-112` 明确拒绝非 postgresql），SQLite 支持已移除，所以不用考虑 SQLite 的 `ALTER TABLE` 限制。

**注意**：后端的自有表（`ui_*` / `users` / `refresh_tokens`）走 `app/db/database.py` 的 `create_all`，与上面这套 schema.sql 是**两条独立的迁移路径**，不要混。

---

## 12. 配置变更汇总

以 `configs/hardware.yaml` 为准（`base.yaml` / `dev.yaml` 同步；`base`/`dev` 是 `runtime.mode: simulated`）：

```yaml
devices:
  audio:
    device: null
    sample_rate: 44100          # 16000 → 44100（参数冻结）
    channels: 1
    duration_seconds: 10.0      # 3.0 → 10.0
    gain_db: 0                  # 新增：现场整定后填实际值
    silence_rms_threshold: 0.002   # 新增：待整定
    clip_ratio_threshold: 0.001    # 新增

models:
  # package_root: ../model_library/packages   ← 删除（目录不存在，见 §5.4）
  heat_stress: ...              # 不变
  audio_anomaly:                # 二期
    enabled: false
    package: ../models/audio_anomaly/1.0.0/
    # 或 path: ../models/audio_anomaly/1.0.0/model.onnx
    device: ${MODEL_DEVICE:-cpu}
    sample_rate: 16000          # 模型输入率（≠ 采集率，见 §3.11）

sync:
  enabled: true                 # false → true（P3 落地后打开）
  endpoint: <云端实际地址>        # 现在是 https://cloud.example.invalid/... 占位

archive:                        # 新增段
  root: ../data/archives
  format: wav                   # wav | flac
  window_hour: 2                # 每日上传窗口（写入 outbox.next_retry_at）
  retention_days: 14

media_upload:                   # 新增段（P4）
  mode: presigned               # presigned | disabled
  presign_path: /api/v1/robots/presign
  chunk_size_mb: 8
```

**同步要求**：`configs/schemas/base.schema.json` 要补 `archive`、`media_upload` 与新的 `devices.audio` 字段，否则 `additionalProperties: false` 会导致校验失败。**并且 `app/robot_core/common/config.py` 的 `REQUIRED_SECTIONS`（`:84-86`）与 `:98` 的"未知 section 白名单"要同步**——`unknown = set(value) - (REQUIRED_SECTIONS | {...})` 会对新增顶层段直接报错。

**依赖补充**（`pyproject.toml` 的 `[dependency-groups]`）：
- `media` 组加 `soundfile`（FLAC 归档；需系统 `libsndfile1`）
- `inference` 组加 `onnxruntime`（§6.2 已确认，与 `torch`/`ultralytics` 并存——热应激仍需要后者）

---

## 13. 云端职责与接口契约（**已确认：外部云平台，另仓实现**）

**已确认**：`app/` 是**跑在机器人上的**（UDS + 端口 8100），**不是云平台**。接收端是**外部云平台，在另一个仓库实现**。

**因此本方案的边界是**：
- 本仓库（robot-backend）**只负责机器人侧**——归档、入队、预签名申请、直传、确认、清理；
- 本方案**只定义契约**（下面三个接口），**云端实现不在本仓库范围内**；
- `configs/hardware.yaml` 的 `sync.endpoint` 需从占位地址换成云端实际地址。

> 注意：既然云端是独立实现，"机器人侧接口"与"云端接口"必须**先对齐契约再各自开工**，否则预签名申请的字段名（camelCase？）很可能两边不一致。建议先冻结下面这份契约。

### 13.1 云端必须提供的接口（机器人侧调用）

1. **`POST /api/v1/robots/presign`** —— 申请预签名上传 URL
   入参：`robotId` + 资产列表（`assetId` / `sha256` / `sizeBytes` / `contentType`）
   出参：每个资产的 `uploadUrl`（预签名 PUT）+ `objectKey` + `expiresAt`
2. **`POST /api/v1/robots/events`** —— 接收事件（**地址已由 `sync.endpoint` 约定**）
   事件类型：一期 `media.asset.created` / `archive.created`；二期 `audio.anomaly.detected`
   幂等键：事件自带 `idempotency_key`（云端必须按它去重——机器人侧是**至少一次投递**）

### 13.2 云端必须实现的职责（不属本仓库）

3. **对象存储**：接收直传的归档包（WAV/FLAC 母版），按 `object_key` 落库
4. **数据集管道**：解包归档 → 校验 `manifest.json` 的 sha256 → 入库为训练集
5. **标注 / 复核工作台**
   → **一期最该先做的云端功能其实是标注/复核工作台，而不是漂亮的告警看板。** 当前阶段数据唯一的用途就是训练，而训练需要标签。**标签缺失是最大隐性风险**：采几万条没标签等于没有数据集。

---

## 14. 测试计划

现有测试入口不变：`uv run pytest`（`pyproject.toml` 的 `testpaths = ["tests"]`、`pythonpath = ["."]`，需要 PostgreSQL 测试库）。

| 阶段 | 新增/调整测试 |
|---|---|
| P0 | `tests/robot_core/unit/test_audio_provider.py` 扩展：44.1 kHz/10 s 默认值、`CaptureResult` 返回、静音→`invalid`、削波→`degraded`、超时、重试、中途停止（用 fake sounddevice 模块，现有测试已是这个模式） |
| P1 | 配置校验（`test_config.py`）：新字段范围、`archive`/`media_upload` 段通过校验 |
| P2 | `build_configured_adapters()` 多模型遍历；音频 adapter 在 fake ONNX 会话下的输出契约 |
| P3 | `Archiver` 打包内容与 manifest sha256；`asset.register` 同事务入队；`next_retry_at` 窗口 |
| P4 | `MediaUploader` 预签名 + 分片续传（fake HTTP）；sync-agent 事件分流 |
| P5 | 保留清理的顺序保护（**未 sent 不得删**） |

---

## 15. 风险与注意

**安全（新增，最高优先级）**
- **`robot.env` 含真实凭据且已被 git 跟踪**（§5.6）。这是当前仓库里最该立刻处理的一件事。

**一期**
- **采集参数现在就要冻结**（§3.10）：数据正在采，中途改采样率或增益只能靠元数据事后筛段，代价极高。
- **模型权重不在 git**（§5.3）：首次裸机部署最容易漏，且漏了会表现为 `inference-worker` 无声循环重启。
- **不要只录"可疑"样本**（§3.7）：异常检测学不出"正常"分布就等于没用。
- **标签缺失是最大隐性风险**（§13）：一期必须同步建立标注/复核流程。
- **`enqueue_outbox()` 无调用方 = 数据不出机器人**（§8.4）：这是当前最大功能断点。
- **上传失败会积压整天**（§9.4）：需要容量规划、积压可见性、连续失败告警。
- **流量成本**：8 GB/月/台 在 4G 计费下不可忽略（§6.1）。
- **磁盘顺序保护**：只在上传成功后才清理；归档包发出前**不要删原始片段**（§10）。
- **分片上传是必须的**：归档包可达上百 MB，单次 PUT 遇断网要能续传。
- **`_expand_environment` 整树 eager 展开**（§5.6）：新增凭据要确认六个单元都有。

**二期**
- **算力预算**：10 秒片段推理耗时必须实测；`inference-worker` 与其他负载抢 CPU 会导致 `deadline_at` 超时（`status="timeout"`）。裸机没有 cgroup 限制（旧容器形态隐含的资源约束消失），但 systemd 可以用 `CPUQuota=` / `MemoryMax=` 给 `inference-worker` 限流。
- **装配失败 = 循环重启**：`models` 配置错误会让 `inference-worker` 启动即抛——systemd 下是 `Restart=on-failure` 循环，**先看日志第一处异常**，并考虑配 `StartLimitIntervalSec=` / `StartLimitBurst=` 让配置错误快速失败停住。
- **模型包版本一致性**：换模型不换特征参数是最隐蔽的翻车方式。manifest 带 `training_dataset_version`，事件回传 `model_sha256` 用于归因。
- **ONNX 导出必须做数值对齐测试**（§6.2）。
- **采样率不可逆**：这是选 44.1 kHz 的根本原因。

**形态（裸机）**
- **没有镜像 = 没有原子回滚**（§5.7）：用 git tag + 重跑 `install`，或 `releases/` + 软链。
  **反过来说这是好消息**：模型更新不再需要重建镜像，模型包可独立灰度下发。
- **裸机下 PostgreSQL 的去向**：`deploy/postgres-compose.yml` 是旧 Docker 形态的产物；既然不用镜像，**PG 应装到宿主机**，并且六个单元要补 `After=postgresql.service`（§5.5）。
- **`schema` 迁移不可逆**：加列向后兼容，但降级需人工处理；`_upgrade_existing_tables` 是唯一的补列路径（§11.2）。

---

## 16. 落地顺序

### 一期：采集 + 数据集回流（**现在就开始**）

| 阶段 | 内容 | 交付物 |
|---|---|---|
| **P1-a** | **安全问题**：`.gitignore` + `git rm --cached robot.env` + 轮换凭据 | `.gitignore`、`robot.env` |
| P0 | 流式采集 + 质量检测 + 重试 + `CaptureResult` + 44.1 kHz/10 s/固定增益参数冻结 | `app/robot_core/sensing/{audio,service}.py`、`app/robot_core/contracts/models.py`、三份配置、`tests/robot_core/unit/test_audio_provider.py` |
| P1-b | 声卡权限（`SupplementaryGroups=audio`）、系统库预检、模型权重校验、`package_root` 清理、单元注释与 `After=postgresql.service` 纠错 | `deploy/sensor-worker.service.in`、`deploy/inference-worker.service.in`、`deploy/*.service.in`、`deploy/quick_start_up.sh`、三份配置 |
| P1-c | 模拟模式不再冒充 WAV（返回合法最小静音 WAV）、`MediaCaptureRequest` 参数范围校验 | `app/robot_core/processes/sensor_worker.py`、`app/schemas/media.py` |
| P3 | 任务级归档（`Archiver`）+ 归档资产落库 + outbox 入队（`next_retry_at` 窗口）+ `sync.enabled` 打开 + `audio_events` 空表 | `app/robot_core/storage/archive.py`、`app/robot_core/processes/robot_core.py`、`storage/schema.sql` + `database.py`、配置 |
| P4 | `MediaUploader` 预签名 + 分片直传 + sync-agent 分流 + 云端 presign/events 契约对接 | `app/robot_core/sync/media_upload.py`、`app/robot_core/processes/sync_agent.py`、配置 endpoint |
| P5 | 保留清理（带顺序保护）+ **云端数据集管道与标注/复核工作台**（另仓） | `app/robot_core/storage/retention.py`、schema 迁移、云端 |

**一期完成即产出可训练的数据集**，可以开始训练模型。

### 二期：本体推理（模型训练完成、导出 ONNX 后启动）

| 阶段 | 内容 |
|---|---|
| P2-a | `AudioAnomalyAdapter` + `build_configured_adapters()` 重构 + 模型包 |
| P2-b | 编排接入（`inspection-worker`）+ `audio_events` 写入 + `alerts` 复用 |
| P2-c | 采集→推理闭环联调 + 算力实测 + `CPUQuota=` 限流 |
| P2-d | 优先级上传（异常片段立即传） |

---

## 17. 后续演进

- **连续采集 + 触发切片**：替代固定 10 秒片段，用 VAD/事件检测定位咳嗽起止——**咳嗽是短事件，整段打一个标签会把它稀释掉**。
- **设备噪声抑制**：谱减 / 自适应滤波 / 针对风机基频的梳状滤波，全部在分析端离线做。
- **频带分离 + 噪声底估计**：把鸡叫从宽带机械噪声里剥出来。
- **分层数据集**：按 `coop_id / point / layer / 设备运行状态` 分层组织，支持分层建模与偏差排查。
- **数据集版本化**：冻结采集参数后打版本号，与模型版本绑定。
- **凭据收敛**：`LoadCredential=` 按进程拆分（§5.6）。
- **`_expand_environment` 惰性化**：从根本上消除"新增凭据要同步所有单元"的维护成本。
