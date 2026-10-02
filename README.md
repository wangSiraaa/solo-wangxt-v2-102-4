# 频谱工作台（通信工程教学 · 离线简化模型）

用于比较一组载波频率配置的教学工具：录入少量载波与保护间隔后，绘制频段占用与发射谱，
检查 **频带重叠、保护带不足、掩模尾部越界**，在**线性域 (W)** 汇总功率后以 dBm 显示；
极化复用由输入规则决定（禁止 / 允许 / 隔离度未知待评估）；并可用 **OR-Tools CP-SAT**
为载波寻找一组满足间隔的频率位置。

> ⚠️ 纯离线简化模型：**不连接无线电设备，不生成任何发射指令**。掩模与发射谱均为教学
> 用平顶 + 折线衰减的简化数值，不对应任何标准或真实设备指标。

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | React 18 + Vite + Plotly.js |
| 后端 | FastAPI + Pydantic + SQLAlchemy 2 |
| 科学计算 | SciPy（掩模分段线性插值、PSD 积分）、NumPy |
| 约束求解 | OR-Tools CP-SAT（1 kHz 频率网格） |
| 数据库 | PostgreSQL（中心频率、带宽、功率、示例掩模、场景） |

## 目录

```
backend/   FastAPI 应用
  app/services/units.py     dBm<->W、线性域功率汇总
  app/services/masks.py     掩模折线、发射谱曲线（SciPy interp1d）
  app/services/analysis.py  三类冲突检查（定位到载波对/方向）
  app/services/planner.py   OR-Tools CP-SAT 频率规划
  app/services/snapshots.py 场景快照、SHA-256 内容哈希、提案结构化差异
  app/services/proposals.py 版本化调频提案工作流（状态机/乐观并发/post-check/审计）
  app/proposals_api.py      提案与场景版本的 FastAPI 路由
  app/db.py / seed.py       SQLAlchemy 模型与教学演示场景
frontend/  React + Plotly.js
scripts/   start-dev.sh     一键启动（本地 PostgreSQL + 后端 + 前端）
```

## 快速开始

需要本机 PostgreSQL（或修改 `DATABASE_URL`）。提供一个 conda 方式安装的便捷脚本：

```bash
# 1) Python 依赖
pip install -r backend/requirements.txt

# 2) 前端依赖
cd frontend && npm install && cd ..

# 3) 初始化数据库并写入示例掩模 + 教学演示场景
python -m app.seed          # 工作目录为 backend/，或设置 PYTHONPATH

# 4) 启动
( cd backend && uvicorn app.main:app --reload --port 8000 )
( cd frontend && npm run dev )        # http://localhost:5173 ，/api 代理到 8000
```

环境变量：`DATABASE_URL`（默认 `postgresql+psycopg2://postgres@127.0.0.1:5432/spectrum`）、
`CORS_ORIGINS`。

## 计算模型要点

### 1. 冲突检查（`POST /api/analyze`，结果含 bands / spectrum / findings / power_summary）

- **频带重叠 overlap**：两频带边缘净距 `gap = max(low) − min(high) ≤ 0`，报重叠量。
- **保护带不足 guard_shortfall**：`0 < gap < guard_required_mhz`，报净距与缺口。
- **掩模尾部越界 mask_tail（有方向）**：发射机掩模谱落入受害载波频带的功率
  `P = ∫_victim 10^(PSD_dbm/10)·1mW df`（PSD 在线性 W/Hz 域积分），超过限值即报；
  因功率/掩模不同，A→B 与 B→A 分别计算、分别定位。
- **极化复用规则**（针对异极化对）：
  - `forbidden` 禁止：重叠即冲突；
  - `allowed` 允许（已知隔离度足够）：可同频，不报几何/泄漏冲突；
  - `unknown` 待评估：重叠只提示“隔离度未知，复用待评估”，不下违规定性；
  - 同极化对无极化隔离，始终按禁止处理。

### 2. 功率汇总

dB 是对数尺度，**不能直接相加**。先换算
`P_W = Σ 1mW · 10^(p_i/10)`，再 `P_dBm = 10 log10(P_W/1mW)`。
响应里同时给出错误的“dBm 直接求和”数值供课堂对比（例如多个载波 275 dBm vs 正确 36.14 dBm）。

### 3. OR-Tools 规划（`POST /api/plan`）

CP-SAT 在 1 kHz 网格上为每个载波选中心频率，约束：

- 落在给定可用频段内；
- 需要隔离的载波对（同极化 / 禁止 / 待评估）通过布尔析取排序，
  满足 `中心距 ≥ 半宽_i + 半宽_j + 要求边缘净距`；
- 允许复用的极化对不加约束（可同址）；
- `guard_only`：统一用保护间隔；`mask_aware`：对每对载波按**双向**掩模泄漏都不越限
  反算所需净距（含 0.5 dB 规划裕量，保证返回方案在分析口径下必然达标）；
- 目标：最小化相对录入位置的总偏移。无解时返回 `INFEASIBLE` 与放宽建议。
  返回中带 `post_check`（对方案重新跑一遍完整分析）。

## 教学演示场景（种子数据，12 个载波，带宽均为 4 MHz、功率不同）

| 载波对 | 现象 |
|---|---|
| C1(30 dBm,loose) / C2(20 dBm,loose)，净距 0.5 MHz | 保护带不足 + 双向尾部越界，泄漏 15.4 vs 5.3 dBm（功率不同导致方向不对称） |
| C4(30,strict) / C5(25,loose)，净距 0.5 MHz | 保护带不足；仅弱载波 loose 尾部越界（强→弱 −8.6 dBm 达标边界，弱→强 10.3 dBm 越界） |
| C7(30,loose) / C8(20,strict)，净距 2 MHz | 保护带虽够，强载波拖尾仍越界 2.0 dBm；反向 −47.5 dBm 达标 |
| C9 / C10，重叠 0.5 MHz | 同极化频带重叠 |
| C1(H) / C6(V) 同频 | 规则 unknown → “复用待评估” |
| C11(RHCP) / C12(V) 同频 | 规则 allowed → 无冲突 |

## 版本化调频提案（教学基准保护）

教师/学生可以为**现有场景**提出调频方案、反复比较差异，但未经确认的草稿**绝不覆盖教学基准**。
前端在「调频提案（版本化）」页签操作；后端复用现有分析与规划服务，无状态的
`POST /api/analyze`、`POST /api/plan` 直接分析行为保持不变。

**快照与哈希**：场景内容（载波中心频率/带宽/功率/极化/掩模 + 保护间隔 + 泄漏限值 +
极化复用规则 + 可用频段）经规范化（键排序、浮点统一到 1 Hz 精度）后取 SHA-256。
提案创建时锚定基准 `base_version + base_snapshot_hash`；每次草稿改动产生新修订，
分析/规划产物都绑定**输入快照哈希 + 修订号**。

**状态机**：

```
draft ──review──▶ reviewed ──apply──▶ applied ──rollback──▶ rolled_back
  ▲                  │
  └──── reopen ──────┘
draft / reviewed ──cancel──▶ cancelled
```

**应用（`POST /api/proposals/{id}/apply`）的三道闸，任一失败整体拒绝、零部分写入**：

1. **乐观并发校验**：场景 `version` 必须仍等于提案锚定的 `base_version`，且当前内容哈希
   等于 `base_snapshot_hash`（基准被他人直接修改即 409 `baseline_conflict`）；
   写入用 `UPDATE ... WHERE version=:base` 条件更新兜底检查—写入之间的并发；
2. **规划有效性**：最新修订必须存在规划产物，且其 `input_snapshot_hash` 等于该修订快照哈希；
   草稿改动后旧修订的规划立即失效（409 `plan_expired`），必须重新规划并重新采纳；
3. **post-check**：应用前对提案目标内容重跑一遍完整 `analyze`，存在 error 即
   409 `post_check_failed`，事务回滚。

被拒绝的应用尝试也写一条 `apply_rejected` 审计事件。重复应用/取消/回退请求幂等：
已处于目标状态时直接返回，不产生新版本、不重复写事件。回退把场景恢复为提案锚定的基准
内容并产生 `rollback` 版本快照（若应用后场景又被推进，则拒绝回退而非覆盖他人基准）。

**差异**记录四类教学关注点：载波频率移动（含偏移量）、掩模变化、极化复用规则变化、
保护间隔/泄漏限值/频段变化；修订差异相对上一修订，同时保留相对基准的累计差异。

主要接口：

| 方法 路径 | 说明 |
|---|---|
| `POST /api/proposals` | 从当前场景创建草稿快照（`content` 缺省=复制基准） |
| `PUT /api/proposals/{id}` | 草稿修订（仅 draft；内容相同拒绝） |
| `POST /api/proposals/{id}/analysis` / `/plan?mode=` | 在当前修订上生成绑定哈希的分析/规划产物 |
| `POST /api/proposals/{id}/accept-plan` | 采纳当前修订规划为新修订（在新修订上重跑分析/规划） |
| `POST /api/proposals/{id}/review` `/reopen` `/cancel` `/apply` `/rollback` | 状态迁移（均幂等/带审计） |
| `GET /api/scenarios/{id}/versions[/{v}]` | 场景版本链（baseline/proposal_applied/rollback）与版本间差异 |
| `GET /api/proposals/{id}/export` | 导出审计包：原基准快照、已应用版本快照、完整版本链、全部产物与每次决定依据 |

## 测试

```bash
# 纯函数 + 规划 + API（API 测试用内存 SQLite，无需外部服务）
PYTHONPATH=backend pytest backend/tests -q
```

33 个测试覆盖：dBm/W 换算、三类冲突对定位、泄漏方向性、三种极化规则、
两种规划模式的可行性与规划后零越界、场景 CRUD，以及版本化提案的
创建/多频点移动/评审/应用/post-check、基准并发冲突拦截、旧规划哈希失效、
取消/重复应用/回退幂等、审计导出等。
