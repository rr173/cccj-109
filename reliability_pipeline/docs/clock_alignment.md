# 离线时钟对齐：锚点、分段映射与误差范围

`alignment/` 是一个**离线排查工具**：把遥测库中已有的测量，按一份同步锚点
表映射到共同参考钟，用于判断跨设备事件的先后关系。

它**不做**以下事情：

- 不改接入器、不改解码/时钟语义，不写回原始时间（数据库以 `read_only`
  打开，不建表、不落结果库）；
- 不推导启机或故障结论（那是 `startup/` 与 `pipeline/` 的职责）；
- 不宣称能覆盖任意未建模的设备跳钟：给出的误差只是**分段线性模型**下
  由锚点误差与斜率上限导出的范围。

> 背景：`tele_samples.ts_ns` 是 `epoch_ns + floor(cum_tick·10⁹ / tick_hz)`，
> 只由设备**自己声明**的起点与频率换算。两台设备的读数即使 `ts_ns` 相同，
> 也不能据此断言它们发生在同一时刻——正是本工具要解决的问题。

---

## 1. 锚点 CSV

同步人员提供，表头恰为（按名取列，列序可变）：

```csv
anchor_id,device_id,boot_seq,local_ns,reference_ns,error_ns
a-001,2,1,0,1700000000500000000,2000000
a-002,2,1,1700000004000000000,1700000004500400000,2000000
```

| 列 | 含义 |
|---|---|
| `anchor_id` | 锚点标识；**文本**身份比较（`001` ≠ `1`），重复则拒绝 |
| `device_id` / `boot_seq` | 分组键；必须为非负整数 |
| `local_ns` | 本地时刻，与测量的 `ts_ns` **在同一坐标系** |
| `reference_ns` | 共同参考钟读数 |
| `error_ns` | 该锚点的非负整数误差半径 |

所有时间字段为**十进制整数纳秒**，允许负值与任意前导 `+/-`；
不接受浮点、科学计数、空串、下划线或超出 int64 的数值。

### 输入阶段的拒绝

- 文件级（整份不采用）：缺表头、重复列、多列/缺列、CSV 解析失败；
- 行级（逐行列出 1-based 行号，整份不采用）：字段数不符、空字段、
  非法整数、`error_ns < 0`、`device_id`/`boot_seq` 为负、**重复
  `anchor_id`**（保留首次、指出第二次）；
- 空行忽略；`anchor_id` 两端空白给出提示并按去空白后的文本处理。

---

## 2. 分组与校验（按 `(device_id, boot_seq)`）

1. **同刻折叠**：同一 `local_ns` 的锚点只有 `reference_ns` 与 `error_ns`
   **完全一致**才折叠为一个锚点，输出仍列出组内全部 `anchor_id`；否则
   该组整体 `无效`（`同一本地时刻…不一致`），不尝试挑一个"看起来对"的。
2. 折叠后每组 **≥ 2 个锚点**；
3. `local_ns` 与 `reference_ns` 均**严格递增**；
4. **斜率检验**：相邻段斜率与 1 的偏差超过 `max_ppm`（非负整数，默认
   1000）时，**整组无效**——绝不跳过坏段后把其余锚点连起来。斜率用精确
   有理数：

   ```
   |(y1−y0)/(x1−x0) − 1| = |dy − dx| / dx  ≤  max_ppm / 10⁶
   ```

   检验用锚点的**中心值**（`reference_ns` 本身）。

任一组无效只影响该组；其他组照常对齐。

---

## 3. 分段仿射映射

设一组折叠后的锚点 `(x_i, y_i, e_i)`，`x` 为待映射的本地时刻，
`a = (x − x0)/(x1 − x0)`：

| 情形 | 段 | 中心 `y` | 半径 `u` |
|---|---|---|---|
| 段内（含端点） | `x ∈ [x0, x1]` | `(1−a)y0 + a y1` | `(1−a)e0 + a e1` |
| 落在锚点本身 | — | `y_i` | `e_i`（不内插、不掺入相邻段） |
| 左/右外推 | 只用最近一段 | 同上 | `abs(1−a)e0 + abs(a)e1 + d·max_ppm/10⁶` |

- 外推时 `d` = 到最近端点（左端用 `x0`、右端用 `x1`）的距离，必须
  `d ≤ max_extrapolation_ns`（非负整数，默认 10⁹）。恰好等于额度允许，
  超过 1 ns 即拒绝。
- 锚点本身即使在外推方向上，也走"锚点自身中心/误差"，`mode` 标为
  `interp`，`anchors_used` 只列该锚点的全部 `anchor_id`。
- 误差范围是**上述模型**下的范围（锚点误差 + 未建模漂移按 ppm 折算），
  不代表任意设备跳钟都被覆盖。

**全部用 `Fraction` 或整数计算，不先转浮点。**

### 舍入

```
corrected_ns = floor(y)
lower_ns     = floor(y − u)
upper_ns     = ceil(y + u)
```

负值按数学定义（`floor(−1.5) = −2`、`ceil(−1.5) = −1`）；误差范围**永不
向内舍入**：只要真实值落在 `[y−u, y+u]`，就一定落在
`[lower_ns, upper_ns]`。

---

## 4. 未对齐

以下情况返回**未对齐 + 具体原因**，输出中 `corrected_ns/lower/upper` 均为
空，绝不用本地时刻冒充参考钟：

- 该 `(device_id, boot_seq)` 没有锚点组；
- 该组无效（折叠矛盾、数量不足、非严格递增、斜率超限……），原因逐字给出；
- 本地时刻超出 `max_extrapolation_ns`（原因含距离与额度）。

---

## 5. 输入到输出的路径

```
tele_samples（只读、keyset 分页、每批默认 4096 行）
      │  (file_id, frame_offset, ordinal, channel) = 原测量定位键
      ▼
align_rows（逐批；每组一个 Mapping）
      ▼
AlignedRow: local_ns → center/radius (Fraction) → corrected/lower/upper
            mode（interp / extrap-left / extrap-right）
            anchors_used（该段两端的全部 anchor_id）
            value、unit、reason（未对齐时）
```

稳定次序：`(device_id, boot_seq, ts_ns, file_id, frame_offset, ordinal,
channel)`。**重复时间的不同测量全部保留**（同帧多通道靠 `ordinal`，
不同来源靠 `file_id/frame_offset`）；不合并、不去重、不挑"最近一条"。

---

## 6. 公开命令

```bash
# 锚点组校验（折叠、递增、斜率），排查输入
python3 -m alignment.cli anchors --anchors sync.csv --max-ppm 1000

# 选定设备的完整投影（每条测量一行，含未对齐原因）
python3 -m alignment.cli project --db outputs/telemetry.duckdb \
    --anchors sync.csv --device 2 --boot-seq 1 \
    --max-ppm 1000 --max-extrapolation-ns 1000000000

# 按参考钟窗口 [L,R] 选取测量：校正范围相交即保留
python3 -m alignment.cli window --db outputs/telemetry.duckdb \
    --anchors sync.csv --low-ns 1700000001998150000 \
    --high-ns 1700000002002150000

# 一条命令跑通示例与回归核对
python3 -m alignment.cli demo --workdir outputs/alignment_demo
# 或 ./run_alignment_demo.sh
```

常用参数：

| 参数 | 默认 | 含义 |
|---|---|---|
| `--max-ppm` | 1000 | 斜率偏离 1 的上限（非负整数 ppm） |
| `--max-extrapolation-ns` | 10⁹ | 允许外推的最大距离 |
| `--batch` | 4096 | 每批读取行数（内存上限与之成正比） |
| `--device` / `--boot-seq` | 全部 | 只投影/选取指定组 |
| `--local-start-ns` / `--local-end-ns` | 全部 | 按**本地**时间段预筛（闭区间） |
| `--json` | 关 | 逐行 JSON，含精确有理数 `center`/`radius` 与定位键 |
| `--no-value` | 关 | 表格省略物理值/单位 |

`window` 的语义：

- 窗口是**闭区间** `[L, R]`；只要校正范围 `[lower, upper]` 与它相交就保留
  （`lower ≤ R 且 upper ≥ L`），**不只看中心**；
- 输出明确写出「扫描 N 条、窗口内 M 条、未对齐/无法判断 K 条」；
- 未对齐的 K 条**不是窗外**：逐条列出原因（含前 10 条示例），归属未知。

退出码：`0` 成功（包括"全部无法判断"）；`2` 锚点输入/配置非法；
`1` 测量库无法只读打开或缺少 `tele_samples` 表。

空间开销：锚点可整体驻留内存（每组 O(锚点数)）；测量**流式**处理，
每批 O(`--batch`) 行；不构造新的永久结果库，也不写归档文件。

---

## 7. 示例与回归

`alignment.demo`（`run_alignment_demo.sh` 或 `demo` 子命令）用**真实二进制
接入命令**生成材料，再只读对齐：

| 设备/周期 | 本地钟 | 示例用途 |
|---|---|---|
| dev 2 boot 1 | 慢 0.5 s，+100 ppm | 与 dev 3 的共同事件 |
| dev 2 boot 2 | 另一周期、偏移不同 | 周期隔离 |
| dev 3 boot 1 | 快 2 s，−50 ppm | 与 dev 2 的共同事件（范围相交） |
| dev 4 boot 1 | 斜率≈1 | 左/右有限外推、超额度 |
| dev 5 boot 1 | epoch = 9×10¹⁸ | 相邻 1 ns 的整数插值 |
| dev 6 boot 1 | epoch = −9×10¹⁸ | 负基数 1 ns |

演示逐项与 Fraction 手算比对，并覆盖：共同事件范围相交、锚点自身、左右
外推、超额度拒绝、矛盾锚点整组无效、斜率超限整组无效、输入重排、非法行
拒绝、周期隔离、窗口按范围相交（中心在窗外但下界擦边）、1 ns 不被吞。

`tests/test_alignment.py` 进一步覆盖：段边界、外推额度恰好/超 1 ns、
闭区间窗口端点、负坐标 floor/ceil、分批读取一致性、只读库（文件哈希
不变）、定位键回到真实 `tele_samples` 行、缺少 `tele_samples` 表等。

**关于"浮点吞掉 1 ns"**：float64 在 9×10¹⁸ 附近的间距约 1024，相邻两个
整数在浮点下完全相同。因此本工具从读库到映射全程整数/`Fraction`，
`Big` 基数下的 `x+1` 与 `x+2` 仍精确可分辨。

---

## 8. 与其他入口的关系

- `telemetry`（接入/查询）、`startup`（启机识别）、`pipeline`（生存分析）
  的现有命令与语义**保持不变**；本工具只新增 `alignment` 子包与
  `run_alignment_demo.sh`；
- 它不调用 `startup` 的生产映射/判定函数，也不写任何 `tele_*`/`startup_*`
  表；回归材料中的期望值独立手算，不通过被测实现生成。
