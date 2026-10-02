# 频谱工作台（通信工程教学 · 离线简化模型）

用于比较一组载波频率配置的教学工具：录入少量载波与保护间隔后，绘制频段占用与发射谱，
检查 **频带重叠、保护带不足、掩模尾部越界**，在**线性域 (W)** 汇总功率后以 dBm 显示；
极化复用由输入规则决定（禁止 / 允许 / 隔离度未知待评估）；并可用 **OR-Tools CP-SAT**
为载波寻找一组满足间隔的频率位置。

此外提供**版本化“调频提案”**工作流：学生从教学基准拉出草稿快照，在草稿上反复分析/规划，
教师评审后应用；未经确认的草稿绝不会覆盖基准，应用时还有乐观并发与服务端 post-check 双重把关。

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
  app/services/snapshots.py 提案快照归一化、SHA-256 哈希、四维差异（纯函数）
  app/proposals_service.py  提案状态机 / 乐观并发 / 原子应用 / 审计导出
  app/proposals_api.py      提案与基准版本的 FastAPI 路由
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

### 4. 版本化调频提案（草稿 → 评审 → 应用 / 取消 / 回退）

学生为“现有频率场景”提出调频方案、教师比较差异并决定是否采纳。**草稿永不写基准**。

状态机：

```
draft ──review──▶ reviewed ──apply──▶ applied ──rollback──▶ rolled_back
  ▲                │                  （重复 apply/rollback/cancel 均幂等）
  └ 草稿再次修订 ──┘
draft/reviewed ──cancel──▶ cancelled
```

- **快照与差异**：创建提案时锁定基准 `base_revision / base_hash / base_content`
  （`scenario_versions` 不可变版本链的 revision 0）；草稿每次保存追加不可变
  `proposal_snapshots`，差异覆盖**载波频率移动、掩模、极化复用规则、保护间隔**
  （另含泄漏限值、可用频段、载波增删与属性变化）。
- **哈希绑定**：快照内容先按键排序、浮点按 6 位归整后取 SHA-256。
  每次 `/analyze`、`/plan` 产物都记录 `input_hash + revision`；
  草稿一改动，哈希与修订号变化，旧分析/规划立即在服务端判定失效
  （评审通过后再改草稿会自动退回 draft）。
- **应用的三道闸（任一不过即整体拒绝，同一事务回滚，绝不部分写入频率）**：
  1. 乐观并发：基准 `current_revision + 内容哈希` 必须与提案基线一致
     （PostgreSQL 上对场景行加 `FOR UPDATE` 行锁）；
  2. 规划新鲜度：必须存在绑定当前草稿哈希/修订号、且 `feasible` 的规划，
     载波集合还要对得上；
  3. 服务端 post-check：按规划结果重建目标场景，重跑完整 `analyze`，
     出现任一 `error` 即拒绝（因此仅“保护间隔”模式的方案若掩模尾部仍越界会被拦下）。
  拒绝本身作为 `apply_rejected` 事件单独提交，审计不丢。
- **取消 / 回退**：取消只终态化、不动基准；回退把基准恢复为提案创建时快照，
  并在版本链追加新的 `rollback` 版本（内容哈希与原基准相同，修订号继续前进）。
  应用前/后的基准版本、决定流水（`proposal_events`，只追加）全部保留。
- **幂等**：重复 apply（已应用）、cancel（已取消）、rollback（已回退）返回当前状态且
  不重复记事件；非法跃迁返回 409。
- **刷新 / 导出**：`GET /api/proposals/{id}/export` 同时包含原基准快照、
  应用与回退后的基准版本链、每次草稿差异、绑定哈希的分析/规划依据与可回放事件文本。
- 直接分析行为（`POST /api/analyze`、`POST /api/plan`）保持无状态、无变化；
  直接编辑基准（`PUT /api/scenarios/{id}`）在内容真正变化时追加一条 `baseline` 版本。

提案相关端点：

| 方法 & 路径 | 作用 |
|---|---|
| `POST /api/proposals` | 从场景创建草稿（可带初始内容，默认复制当前基准） |
| `PUT /api/proposals/{id}/draft` | 保存草稿修订（旧分析/规划随即失效） |
| `POST /api/proposals/{id}/analyze` | 对当前草稿跑完整分析并绑定哈希 |
| `POST /api/proposals/{id}/plan` | guard_only / mask_aware 规划（含 post-check） |
| `POST /api/proposals/{id}/review` | 教师评审（须先有当前快照分析） |
| `POST /api/proposals/{id}/apply` | 乐观并发 + 规划新鲜度 + post-check，原子应用 |
| `POST /api/proposals/{id}/cancel` / `rollback` | 取消 / 回退（幂等） |
| `GET /api/proposals[?scenario_id=]` / `/{id}` / `/{id}/export` | 列表 / 详情 / 审计导出 |
| `GET /api/scenarios/{id}/versions[/{rev}]` | 基准版本链 / 指定版本完整内容 |

前端新增“调频提案”页：差异表（移动/掩模/极化规则/保护间隔）、状态色标、
分析与规划的哈希绑定提示、基准版本链查看器、审计时间线与“导出审计包 JSON”。

## 教学演示场景（种子数据，12 个载波，带宽均为 4 MHz、功率不同）

| 载波对 | 现象 |
|---|---|
| C1(30 dBm,loose) / C2(20 dBm,loose)，净距 0.5 MHz | 保护带不足 + 双向尾部越界，泄漏 15.4 vs 5.3 dBm（功率不同导致方向不对称） |
| C4(30,strict) / C5(25,loose)，净距 0.5 MHz | 保护带不足；仅弱载波 loose 尾部越界（强→弱 −8.6 dBm 达标边界，弱→强 10.3 dBm 越界） |
| C7(30,loose) / C8(20,strict)，净距 2 MHz | 保护带虽够，强载波拖尾仍越界 2.0 dBm；反向 −47.5 dBm 达标 |
| C9 / C10，重叠 0.5 MHz | 同极化频带重叠 |
| C1(H) / C6(V) 同频 | 规则 unknown → “复用待评估” |
| C11(RHCP) / C12(V) 同频 | 规则 allowed → 无冲突 |

## 测试

```bash
# 纯函数 + 规划 + API（API 测试用内存 SQLite，无需外部服务）
PYTHONPATH=backend pytest backend/tests -q
```

35 个测试覆盖：dBm/W 换算、三类冲突对定位、泄漏方向性、三种极化规则、
两种规划模式的可行性与规划后零越界、场景 CRUD，以及版本化调频提案的完整验收
（多移动提案评审应用与新基准完整分析、基准被他人修改时冲突拦截且无部分写入、
草稿改动使旧规划哈希失效必须重新规划、重复应用/取消/回退幂等、post-check 失败整体拒绝、
并发提案冲突、老库迁移与导出审计）。
