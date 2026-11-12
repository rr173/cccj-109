# 工业设备可靠性生存分析管线

从追加式观察日志产出**可核验**的生存分析队列与估计结果。
技术栈：Python 3.11+、DuckDB（持久化与查询）、NumPy（估计递推）。无图形界面。

## 快速开始

```bash
pip install -r requirements.txt   # numpy, duckdb
./run_demo.sh                     # 一条命令：日志生成 → 分析 → 核对 → 偏差展示
```

`run_demo.sh` 等价于 `python3 -m pipeline.cli demo`，依次执行：

1. 生成手算金标准日志（`data/gold/`）；
2. `gold_run1`：`knowledge_cutoff=2024-02-15, observation_end=2024-01-31` 的分析；
3. `gold_run2`：`knowledge_cutoff=2024-02-28` 的分析（D05 的迟到撤销此时可见）；
4. 金标准核对（内存重算 + 库中已存结果比对，证明迟到撤销不改写历史）；
5. 竞争风险偏差展示（AJ 的 CIF vs 误作删失的 KM）；
6. 15 个随机种子的「主估计器 vs 逐日枚举参考」交叉核对 + 不变量检查。

单步命令：

```bash
python3 -m pipeline.cli gen-gold --out data/gold
python3 -m pipeline.cli analyze --data data/gold --db outputs/reliability.duckdb \
    --run-id gold_run1 --knowledge-cutoff 2024-02-15 --observation-end 2024-01-31
python3 -m pipeline.cli verify-gold --data data/gold --db outputs/reliability.duckdb
python3 -m pipeline.cli verify-random --seeds 20
python3 -m pipeline.cli show-bias --db outputs/reliability.duckdb --run-id gold_run1 --group ALL
python3 -m pytest tests/          # 可选：pytest 形式的金标准与随机核对
```

## 数据模型

**设备基础资料**（`devices.csv`）：`device_id, machine_type, commission_date（投产日）, enroll_date（观测入组日）`。

**事件日志**（`events.csv`，追加式，禁止覆写）：
`event_id, device_id, event_type, event_date, received_at, voids_event_id, note`

- `event_type ∈ {failure（故障）, retirement（预防性退役）, loss_to_followup（失访）, void（作废）}`
- 更正通过新增 `void` 记录、以 `voids_event_id` 引用旧事件完成，**原记录保留**；
- `received_at` 为事实到达日期，知识截点据此判定可见性。

## 时间约定与风险集

- 时长 = 自投产日起经过的**整日**数：`t = (date − commission_date).days`；
- 进入日 `L = (enroll_date − commission_date).days`，退出日 `T = (terminal_date − commission_date).days`；
- 入组较晚形成**左截断**：结局日 `t` 的风险集为

  ```
  R(t) = { i : L_i < t ≤ T_i } ,   n(t) = |R(t)|
  ```

- 只有 `L < T` 的设备能贡献观察时长；`L ≥ T` 的设备记入清单但标记
  `excluded_no_time`，不参与估计。

## 估计公式

逐结局时点 `t`（当日至少一起故障或退役）统计：故障数 `d₁(t)`、退役数 `d₂(t)`、
删失数 `c(t)`（失访 + 观察截止；当日删失者仍计入当日风险集，结局之后退出）。

**总体生存函数**（两类结局合并）：

```
S(t) = ∏_{s ≤ t} ( 1 − (d₁(s) + d₂(s)) / n(s) )
```

**Aalen–Johansen 累积发生概率**（故障 `j=1`，退役 `j=2`）：

```
CIF_j(t) = Σ_{s ≤ t} S(s⁻) · d_j(s) / n(s)
```

即每个结局时点贡献「当时的条件概率 `d_j(s)/n(s)` 乘以活到那时的概率 `S(s⁻)`」，
**不是**累计故障数除以设备总数。离散递推下恒等式
`S(t) = 1 − CIF₁(t) − CIF₂(t)` 精确成立（不变量核对项之一）。

**对照估计（偏差展示）**：把竞争退役误当普通删失的 Kaplan–Meier：

```
S_KM(t) = ∏_{s ≤ t} ( 1 − d₁(s) / n(s) ) ,   F_KM(t) = 1 − S_KM(t)
```

因 `S(s⁻) ≤ S_KM(s⁻)` 恒成立，有 `F_KM(t) ≥ CIF₁(t)`：退役者被当作
「仍在险、未来仍可能故障」，系统性高估故障累积概率。仅当退役从未先于
`t` 发生时两者相等。`show-bias` 子命令展示二者数值差异。

## 离散同日处理顺序

1. **进入**：`L = t` 的设备在 `t` 日之后才进入风险集（风险集要求 `L < t`）；
2. **结局**：`T = t` 的故障与退役同时计入分子 `d₁(t)、d₂(t)`；
3. **删失**：`T = t` 的删失（失访/观察截止）在结局处理之后退出，
   因此仍计入 `n(t)` 与 `c(t)`；
4. **同一设备同日多个有效终止事件**：固定优先级
   `failure > retirement > loss_to_followup`，再按 `event_id` 字典序决胜；
   落选事件在台账中标记 `superseded_same_day`。

## 数据质量与更正政策

每次分析带两个时间参数：`knowledge_cutoff`（知识截点）与
`observation_end`（观察截止）。

- **知识截点**：仅 `received_at ≤ knowledge_cutoff` 的记录可见。截点后到达的
  事实（包括撤销）对本次分析不可见；已保存的更早分析结果**不会被改写**
  （每次运行以独立 `run_id` 落库，金标准核对会同时比对库中已存结果）。
- **观察窗口**：`event_date > observation_end` 的有效事件不构成结局，
  设备在 `observation_end` 处删失（`censored_end`）。
- **作废解析**：作废目标在截点前不可见 → 该 void 拒收（`void_target_unknown`）；
  作废可指向另一条 void（撤销的撤销，被作废的 void 失去效力）；作废引用
  成环（含自环）→ 环上所有事件拒收（`void_cycle`），其作废边全部忽略。
- **记录级拒收**（进入拒收明细 `rejections`）：日期非法
  （`invalid_event_date` / `invalid_received_at`）、未知事件类型、未知设备、
  事件早于投产日（`event_before_commission`）、作废缺目标、
  `event_id` 重复（保留先出现者，`duplicate_event_id`）。
- **设备级拒收**：基础日期非法、入组早于投产、缺机型、
  **入组前已终止**（`terminated_before_enroll`，取入组后第一项有效终止事件
  时发现更早的有效终止事件）。
- 每台设备取入组后（`event_date ≥ enroll_date` 且 `≤ observation_end`）的
  第一项有效终止事件；故障为关注结局，退役为竞争结局，失访与观察截止为删失。

## 持久化与可追溯

每次分析在 DuckDB（默认 `outputs/reliability.duckdb`）写入：

| 表 | 内容 |
|---|---|
| `runs` | 运行参数（截点、观察截止、代码版本、输入行数） |
| `run_input_devices` / `run_input_events` | 本次分析所用原始输入快照 |
| `run_devices` | 入选/拒收清单 + 设备时间段（L、T、状态、决定结局的 event_id） |
| `rejections` | 拒收明细（级别、原因、说明） |
| `event_audit` | 每条事件的处置台账（选中/同日落选/作废/窗口外/截点后不可见…） |
| `risk_sets` | 逐机型及 ALL、逐结局时点的 n(t)、d₁、d₂、c(t) |
| `risk_set_members` | 每个风险集的设备成员明细 |
| `estimates` | S、CIF₁、CIF₂、KM 对照曲线 |

同时按 `outputs/<run_id>/*.csv` 导出，便于人工核查。任何数字都可沿
`estimates → risk_sets → risk_set_members → run_devices → run_input_events`
回溯到原始事件。

## 核验体系

1. **金标准**（`pipeline/gold_data.py` + `verify-gold`）：13 台设备手算队列，
   覆盖延迟入组、同日故障与退役（跨设备与同设备）、纯删失组、截点后才收到的
   撤销（两次运行对比）、入组前终止、观察期末恰好故障、非法日期、循环撤销、
   入组日即终止日。期望值以精确分数硬编码，逐点比对。
2. **随机交叉核对**（`verify-random`）：种子可复现的随机小队列，
   NumPy 主估计器 vs 独立的逐日枚举 `Fraction` 参考实现逐点比对。
3. **不变量**（`pipeline/checks.py`）：风险人数非负且 ≥ 当日结局数；
   CIF 单调不减；`CIF₁ + CIF₂ ≤ 1`；`S = 1 − CIF₁ − CIF₂`；
   `F_KM ≥ CIF₁`；风险集成员数与 `n(t)` 一致。

## 金标准手算（gold_run1，分组 ALL）

入选 11 台（D06 入组前终止拒收；D11 L=T=30 无观察时长）：

| t | n(t) | d₁ | d₂ | c | S(t) | CIF₁ | CIF₂ | S_KM |
|---|------|----|----|---|------|------|------|------|
| 9  | 10 | 1 | 1 | 0 | 4/5 | 1/10 | 1/10 | 9/10 |
| 14 | 8  | 1 | 0 | 0 | 7/10 | 1/5 | 1/10 | 63/80 |
| 19 | 7  | 1 | 0 | 0 | 3/5 | 3/10 | 1/10 | 27/40 |
| 30 | 6  | 1 | 0 | 5 | 1/2 | 2/5 | 1/10 | 9/16 |

`gold_run2`（截点 2024-02-28，V05 撤销生效）：D05 的故障被作废、转为观察截止
删失，t=19 不再是结局时点，t=30 时 n=7、CIF₁=3/10；而 `gold_run1` 的已存结果
保持不变——撤销只影响其到达之后的分析。
