# 实验分配：整簇随机化（`allocation/`）

在**共用维护班组的设备必须整体接受同一方案**的约束下，为分层随机对照试验
产生精确的实验名单。现场限制是：一个 cluster（班组/机群）可以横跨多个层
（机型 × 站点），整个 cluster 只能进 A 或 B；不能为了凑机型比例把它拆开。
而每个层的 A 组台数又必须**恰好**等于设计文件规定的 `target_A`。

本工具**只产生实验名单**：

- 不更改、不回写任何观察日志（不写 `data/gold`、不写 DuckDB）；
- 不推导故障结局、不算生存曲线、不评估试验效果；
- 不触碰 `pipeline/`、`telemetry/`、`startup/`、`alignment/` 的表或文件。

## 命令

```bash
# 抽签（默认 seed=0）
python3 -m allocation.cli run --devices dev.csv --design design.json \
    [--seed 42] [--max-states 5000000] [--out-dir outputs/alloc_run]

# 按 rank 取唯一方案（rank ∈ [0, Z)）
python3 -m allocation.cli run --devices dev.csv --design design.json --rank 17

# 另一进程只凭规范化设计与 rank 复算（不读 CSV、不重放内部状态）
python3 -m allocation.cli replay \
    --normalized outputs/alloc_run/design.normalized.json --rank 17 \
    [--devices-csv dev.csv]

# 一条命令跑通全部演示与回归核对
python3 -m allocation.cli demo --workdir outputs/allocation_demo
```

退出码：`0` 成功；`2` 输入/设计/rank 非法；`1` **约束无解**（Z=0）；
`3` **未完成精确求解**（`max_states` 预算耗尽）。后三种都**不写任何文件**。

## 输入

### 设备名单 CSV

```csv
device_id,stratum,cluster_id
D001,AX-Site1,crew-7
D002,AX-Site1,crew-7
D003,BX-Site2,crew-7
```

- 表头按名取列（`device_id, stratum, cluster_id`，列序可变，多余列忽略并提示）；
- 三个字段都是非空文本；同一 `device_id` 只能出现一次；
- 标识**按原文本**比较：`001` 与 `1` 是两台不同设备，不做数字归一；
- 行序不影响结果（见「复现性」）。

### 设计 JSON

```json
{
  "target_A": { "AX-Site1": 12, "BX-Site2": 8 },
  "locks": [
    { "cluster_id": "crew-7", "arm": "A", "reason": "共用班组已定" },
    { "cluster_id": "crew-9", "arm": "B" }
  ]
}
```

- `target_A`：**每个实际出现的层**都必须给整数配额；其余设备进入 B；
- `locks`：可选；`arm` 必须恰为 `"A"` 或 `"B"`；同一 cluster 不能重复锁定，
  也不能同时锁到 A 与 B；`reason` 只用于审计展示。

### 拒绝的输入（都指出位置）

| 问题 | 报错位置 |
|---|---|
| 未知层（设计给了配额，名单没有该层） | `target_A[层名]` |
| 缺配额（名单有该层，设计没给） | 层名 + 该层设备数 |
| 配额越界（负数或 > 该层设备数） | `target_A[层名]` |
| 重复 `device_id` | CSV 第 N 行（并指出首次出现行） |
| 未知 cluster 的锁定 | `locks[i]` |
| 错误方案名 | `locks[i].arm` |
| 表头缺列/重复列、空字段、JSON 语法 | CSV 行号 / `path:line:col` |

## 算法

### 规范化

层与 cluster 都按 Python 字符串码点顺序排序；设备按 `device_id` 排序。
CSV 行序与 JSON 键序不参与，因此**同一内容只调换行序，规范化问题不变**。

每个 cluster 用**向量贡献**表示：`v_c = (n_{c,0}, ..., n_{c,k-1})`，即它在
各层的设备台数。决策单位是 cluster，因此设备永远不会被拆开。

### 锁定先纳入配额推理

锁定为 A 的 cluster 贡献直接从各层配额扣除：
`residual = target_A - 锁定A贡献`；锁定为 B 的没有贡献。若某层扣成负数，
直接报告「锁定本身已使该层超额」，并列出贡献来源 cluster 与台数；若剩余
需求超过未锁定设备数，也逐层说明。这些检查都在建 DP 之前完成。

### 精确计数（任意精度整数）

未锁定 cluster 按规范顺序编号 `0..m-1`，`count[j][r]` = 用 cluster
`j..m-1` 恰好填满剩余配额向量 `r` 的方案数：

```
count[m][0] = 1 ;  count[m][r] = 0  (r ≠ 0)
count[j][r] = count[j+1][r]              # cluster j 选 B
            + count[j+1][r - v_j]        # cluster j 选 A（各分量足够时）
```

只保留 `0 ≤ r ≤ residual`（逐分量）且计数为正的状态。`Z = count[0][residual]`。
所有计数都是 Python `int`，绝无浮点。

### 无偏抽签

从 `j=0, r=residual` 起，每一步：

```
zA = count[j+1][r - v_j]      # 选 A 的可行后缀数
zB = count[j+1][r]            # 选 B 的可行后缀数
u  ← uniform_int(0, zA+zB-1)  # 任意精度整数
u < zA ? 选 A : 选 B（并 u ← u - zA）
```

**均匀性推导**：每步的后继方案集合大小恰为 `zA+zB`，被划分为两个互不
相交、大小分别为 `zA`、`zB` 的块；对块大小成比例的整数抽取命中任一块的
概率等于该块占比。一个完整方案对应唯一一条分支路径，因此

```
P(方案) = Π_j 1/(zA_j + zB_j) = 1/Z
```

即每份可行方案等概率。抽签因此**等价于**在 `[0, Z)` 上均匀抽一个 rank 再
按固定字典序解码。方案以 cluster 标记向量区分，不按设备排列数加权，也不
存在「先挑层内方案再拼接」的偏置。

### 固定组序与 rank

组序 = 规范化 cluster 顺序；方案编码为 `A=0, B=1` 的向量，rank 是它在
可行方案中的 0-based 字典序序号（A 在 B 前）。锁定分量在所有方案中相同，
故 rank 在「未锁定 cluster 的 A/B 选择」上仍然连续、不重不漏。给定 rank
按同一分支规则走：`rank < zA` 选 A，否则选 B 并 `rank ← rank - zA`。
越界 rank 在入口拒绝（退出码 2）。

结果里保留 `rank`（`run.json`、`summary.txt` 与 `allocation.csv` 同目录的
`design.normalized.json`），另一进程可只用「设计 + rank」复算：
`python3 -m allocation.cli replay --normalized design.normalized.json --rank R`。
复算会重建 DP 并用同一 rank 解码，不需要序列化抽签过程的内部状态。

### 伪随机种子的用途

`--seed` 只初始化 `random.Random`（Mersenne Twister）来产生上述
`uniform_int`。它不改变 `Z`、组序或 rank↔方案的对应，也不参与方案空间的
定义。相同「规范化输入内容 + seed」在任何进程中给出相同抽签；只调换 CSV
行序也给出相同结果。**只承诺可复现的均匀随机化，不承诺密码学安全**：
seed 不是密钥，对攻击者可预测性有要求的场合不要使用。

## 预算、无解与失败语义

- **Z=0**：明确报告「约束无解」，并给出逐层的锁定贡献/剩余配额说明；
  额外报告**逐层单独看**的配额可达性。如果每层单独都可达，会指出无解来自
  跨层的同一整簇约束。绝不松配额、拆组或换成近似分配。
- **max_states 耗尽**：按实际创建的 DP 状态计数（每阶段每出现一个新状态键
  计 1，复用不重复计）。超限报「**未完成精确求解**」，退出码 3——这不是
  无解。可调大 `--max-states` 重试。
- 两种失败都**不写任何输出文件**；已有的名单文件内容保持原样。
- 写出采用「同目录临时文件 + `os.replace`」：既有文件要么保持旧内容，要么
  整体变成新内容，不会出现半截名单。

## 时间复杂度、空间与适用场景

设层数 `k`，未锁定 cluster 数 `m`，第 `i` 层剩余配额 `R_i`，实际可达
状态数 `S`。

- 时间：`O(m · S · k)` 次整数比较/加法；`Z` 有 `O(m · max_i v_i)` 位，
  大整数加法随位数增长；
- 空间：保留每阶段表以便按 rank 回放，`O(S)` 个状态与大整数；
- 最坏状态上界 `m · Π_i (R_i + 1)`。层数越多、各层配额越宽，越容易爆炸；
  实际 `S` 往往远小于该界（各层由同一批 cluster 供货、设备总数有限）。

状态数达到 `max_states` 的 90% 时，命令会给出「本次已完成、但再扩大很可能
超预算」的提示；这不会改变结果，也不会自动放宽预算。

**适用**：层数不大（个位数到十几）、配额中等、cluster 数在数百以内。
**不适合**：层数很多且每层配额都很宽（例如 20 个层各 20 台自由设备），
此时 `Π(R_i+1)` 迅速超过预算；可先粗化分层，或确认 `n_states` 后再调预算。

## 输出（`--out-dir`）

| 文件 | 内容 |
|---|---|
| `summary.txt` | Z、seed、模式、rank、规范排序、锁定项、逐层计数、逐簇方案、逐设备名单、逐步轨迹 |
| `allocation.csv` | `device_id, stratum, cluster_id, arm, locked` |
| `trace.csv` | 每步：cluster、锁定标记、A/B 完成数、随机数/局部 rank、实际选择、选前/选后剩余配额、逐层贡献 |
| `design.normalized.json` | 与行序无关的规范化设计（层序、组序、配额、逐簇贡献、锁定、Z、rank 顺序），供 `replay` |
| `run.json` | 运行参数、seed、rank、Z、状态数、输入路径 |

逐设备名单会列出每台设备；每个 cluster 的所有设备方案一致，`locked`
列标出锁定组。

## 可复跑材料与核对

`python3 -m allocation.cli demo`（或 `./run_allocation_demo.sh`）生成并核对：

1. **逐层够、合起来无解**：两个跨层整簇，逐层子集和都可达，但 `Z=0`
   （无解来自跨层耦合，而非单层不足）；
2. **含锁定、多解**：`Z=15 = C(6,4)·C(2,2)`；用 `verify.enumerate_feasible`
   显式枚举全部 `2^9` 候选，逐 rank 0..14 核对不重不漏、与字典序一致；
3. **Z > 2^53**：6 层 × 32 个单设备组，`Z = 496^6 = 14,889,840,956,932,096`，
   验证末尾 rank `Z-1` 精确访问、`rank=Z` 被拒绝、抽签全程大整数；
4. **分布检查**：固定小规模 + 大量 seed 的频数统计，仅作均匀性推导的补充；
5. **边界**：空名单、全锁定、零/全额配额、重复设备、`001`≠`1`、锁定冲突、
   锁定超额、非法 rank、预算刚好足够与少一个状态；
6. **文件安全**：无解/非法输入/rank 越界/预算耗尽都不覆盖既有
   `allocation.csv`；调换 CSV 行序结果一致；新进程 `replay` 与 `--rank`
   结果逐行一致。

`tests/test_allocation.py` 以 pytest 形式复刻上述核对（可 `pytest tests/`）。
