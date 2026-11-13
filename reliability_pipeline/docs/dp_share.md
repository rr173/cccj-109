# 差分隐私计数共享（dp_share）

面向合作研究方的**带隐私预算账本的 Laplace 格子计数发布**能力。它是独立的新
研究数据共享能力，**不改动**生存估计、整簇随机分组或遥测计算：对既有 DuckDB
只读访问，隐私账本落在独立的 SQLite 工作区。

- 开通：`python3 -m dp_share.cli provision ...`
- 发布：`python3 -m dp_share.cli publish ...`
- 查账：`python3 -m dp_share.cli ledger ...`
- 一键演示：`./run_dp_share_demo.sh`
- 自跑测试：`python3 -m pytest tests/test_dp_*.py -q`

## 1. 队列、入选状态与三类结局

数据来自既有 DuckDB 的 `run_devices`（结构以真实库 `DESCRIBE run_devices`
为准核验）。开通时选定一个 `run_id`，**只接纳 `cohort_status='included'`
的已入选设备**（`rejected`、`excluded_no_time` 不进入共享队列），取
`device_id`、`machine_type`、`status` 三列构成队列。设备身份按文本处理。

公共结局域**固定**为 `failure / retirement / censored`，真实 `status`
到公共结局的对应：

| run_devices.status | 公共结局     | 含义           |
|---|---|---|
| `failure`       | `failure`    | 故障           |
| `retirement`    | `retirement` | 预防性退役     |
| `censored_loss` | `censored`   | 失访删失       |
| `censored_end`  | `censored`   | 观察截止删失   |

阻断式校验（任何一项失败都拒绝开通，**不静默丢弃**）：

- 入选设备 `device_id` 重复（一台设备只能进入一个机型×结局格子）；
- `status` 无法对应到三类公共结局；
- `machine_type` 落在开通方显式提供的公共机型字典之外；
- 文本列为 NULL/空、run_id 不存在、入选集合为空、表结构缺列。

公共机型字典在开通时由开通方显式给出，**不从私有数据里自动挑选出现过的
机型**；公共格子即使真实人数为零也照样回答。总预算 `epsilon_total_micro`
是正整数，单位百万分之一 epsilon（micro）。

## 2. 机制、敏感度与预算账本

### 2.1 单次发布

研究方提供 `request_id`、机型子集、结局子集与 `epsilon_micro`（正整数）。
子集去重排序后规范化；只允许查询公共域内的格子，不接受任意 SQL 或逐设备
条件。对选中的**完整格子向量**（机型×结局笛卡尔积）：

1. 计算各格子真实人数（含零人数格子）；
2. 每格独立加 `Laplace(0, 1/epsilon)`，`epsilon = epsilon_micro/1_000_000`；
3. 后处理 `reported = max(0, floor(noisy_count + 0.5))`。

后处理不额外消耗预算，也不保证各格报告值之和等于队列人数。

### 2.2 为什么 L1 敏感度是 1、为什么只收一次费

相邻队列定义为“增加或删除一台设备”。每台设备在队列中只属于一个
（机型, 结局）格子：增删一台设备只让该格计数 ±1，其余格子不变，
因此计数向量的 **L1 敏感度恒为 1**（测试 `test_dp_mechanism.py`
对增、删两种相邻队列都断言 L1 距离为 1）。对 L1 敏感度 1 的向量逐维独立
加 `Laplace(1/epsilon)`，整次向量发布满足 **(epsilon)-差分隐私**，
所以**一次向量查询只收一次 epsilon_micro**，不按格子数重复收费；
不同机型也不是互不相关的预算池——所有发布共用同一个总预算。

### 2.3 多次回答与顺序组合

每次新发布是一次独立的 `epsilon_i`-DP 机制。对同一数据集的一系列自适应/
非自适应发布，**基本顺序组合定理**给出总隐私损失上界
`epsilon_total = Σ epsilon_i`。账本把所有发布的费用累加到同一个整数 micro
余额上，并在发布前强制 `spent + epsilon_i ≤ total`，因此账本内的全部回答
整体满足 `epsilon_total`-DP。规范查询（含 epsilon）完全相同的重复请求
**复用原噪声答案且不收费**：这不增加新的机制实例，顺序组合里也不新增
epsilon 项，同时杜绝“靠重试平均掉噪声”。

### 2.4 浮点实现的工程定位

生产噪声源 `SecureLaplaceSource` 用 `secrets`（内核 CSPRNG）在 2^53 整数
网格上做逆变换。实数 Laplace 机制的 DP 定义在 IEEE-754 有限精度舍入下
**不自动继承**；本实现是工程近似，**不宣称形式化的有限精度隐私证明**。
逆变换的端点被显式处理：均匀数严格落在 `[0,1)`，对唯一会导致 `ln(0)`
的 `u=0` 点重采样，保证不产生 NaN、无穷大或 `log(0)`。

## 3. 账本与并发/崩溃语义

SQLite 工作区包含 `workspace_meta / budget / publications /
request_aliases` 四张表。关键不变量：

- **工作区绑定**：队列逻辑指纹（规范化排序后的逐行 SHA-256）、run_id、
  公共机型字典、公共结局域、总预算在开通时固化。工作区文件已存在即拒绝
  再次开通，不能靠换 run_id 或重传配置清零。
- **单事务发布**：新发布的“余额扣减 + 噪声答案 + request_id 关联”在同一个
  `BEGIN IMMEDIATE` 事务里提交，提交后才输出答案。写者经 SQLite 写锁
  串行化，并发进程不可能各自看到同一份余额同时放行（见并发测试）。
- **规范查询复用**：`publications.canonical_query` 有唯一约束。同规范查询
  即使换 request_id 也复用原答案、费用为 0；同一 request_id 携带不同参数
  报 `request_conflict`，不扣款。
- **整数预算**：预算的加减比较全部是整数 micro；`budget` 表有
  `0 ≤ spent ≤ total` 的 CHECK 约束。
- **失败不扣款**：预算不足、输入无效、身份冲突都在生成噪声/写库前拒绝。
- **崩溃语义**：
  - 提交前进程退出 → SQLite 自动回滚，无发布编号、余额不变、答案只存在于
    已死亡进程的内存中，不泄露；原请求重试是一次全新发布。
  - 提交后、输出前退出 → 重试按 request_id 命中已记账发布，拿回同一份答案，
    只扣一次。
  崩溃点由测试装配（`tests/dp_fault_driver.py` + `DP_FAULT` 环境变量），
  发布命令本身没有任何确定性噪声/崩溃开关。
- **源变更冻结**：每次未知 request_id 的新查询前重新抽取源队列并比对指纹；
  不一致就把工作区标记 `frozen` 并拒绝一切新查询，不能在旧账本下换一批人。
  已记账请求的重放只读取账本、不触碰源库，仍可拿回原答案。

## 4. 输出卫生与威胁模型

对外结果只含：发布编号、规范查询、各格子的**带噪且取整截零后的人数**、
本次是否复用、本次扣费、已用/剩余预算。账本里**不存**真实人数、逐设备
资料、原始噪声值或随机源状态；`ledger` 只读视图同样不含这些内容，
设备身份永不出现。

威胁范围：只通过本发布入口查询的合作研究方。不抵抗能直接读取宿主
DuckDB/SQLite 文件的管理员（他们可见原始材料，超出机制边界）。生产随机数
只来自系统安全随机源，研究方无法指定 seed；确定性噪声源仅用于引擎内部
测试注入。

## 5. 命令参考

```bash
# 开通（公共字典显式给出；D 这类零设备机型也保留格子）
python3 -m dp_share.cli provision \
  --db outputs/reliability.duckdb --run-id gold_run1 \
  --machine-types A,B,C,D \
  --epsilon-total-micro 2000000 \
  --workspace /path/to/ws.sqlite

# 发布（子集顺序/重复不影响含义）
python3 -m dp_share.cli publish --workspace /path/to/ws.sqlite \
  --request-id req-001 --machine-types B,A --outcomes censored,failure \
  --epsilon-micro 500000

# 只读查账
python3 -m dp_share.cli ledger --workspace /path/to/ws.sqlite
```

错误以 JSON 写到 stderr 并返回非零稳定退出码：`4` 开通错误、`5` 请求
校验错误、`6` 预算不足、`7` request_id 冲突、`8` 源已变更冻结、
`9` 源不可用、`10` 机制内部错误。

## 6. 测试与独立验证

`tests/dp_helpers.py` 里的验证器**不调用发布实现生成答案**：它直接用
duckdb SQL 重读 `run_devices`、自行做入选过滤与结局映射、自行实现 Laplace
逆变换与取整截零，再与发布产物比对。测试覆盖：

- 可控噪声下的正/负噪声、取整边界、截零、零人数格子；
- 增/删相邻队列的 L1 敏感度 = 1、单向量单收费、尺度与格子数无关；
- 阻断式开通校验（重复设备、未知机型、无法对应结局、坏预算/字典）；
- 复用不收费、request_id 冲突、整数预算、花完预算、输出卫生；
- **真实多进程**并发竞争（不同查询受预算上限约束、同查询只扣一次、
  同 request_id 单一赢家）；
- 装配的提交前/提交后中止点、重启后余额与答案稳定；
- 源队列变化冻结、run_id 更换、再次开通拒绝、非法公共域拒绝；
- 系统安全随机源的经验分布辅助观察（不作为事务/敏感度论证的替代）。
