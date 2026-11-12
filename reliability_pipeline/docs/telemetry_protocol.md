# 传感器遥测接入：字节协议与接入语义

`telemetry` 子包把边缘采集器回传的二进制帧流解码为**带物理单位与准确
Unix 纳秒时刻**的测量样本，事务化写入 DuckDB（`tele_*` 系列表），供研究
人员按设备与通道查询。本入口只处理温度、压力、振动的原始测量解码与入库；
故障结局推导与生存曲线计算属于 `pipeline` 包，不在此进行。

命令行入口：`python3 -m telemetry.cli`（见文末「命令行」）。

## 1. 帧格式

所有多字节数字为**小端**。

```
+------------+-------+----------+----------+------------+
| 帧头 4B    | kind  | len      | payload  | crc32      |
| A7 5C E1 09| u8    | u16      | ≤4096B   | u32        |
+------------+-------+----------+----------+------------+
```

- `crc32` 为 `zlib.crc32`，计算范围是 `kind ‖ len ‖ payload`（**不含帧头**）；
- `kind ∈ {1, 2, 3}`；`payload_len ≤ 4096`；
- 帧最大长度 4+1+2+4096+4 = 4107 字节。

## 2. payload 布局

### kind=3 启动锚点（18 字节）

| 字段 | 类型 | 含义 |
|---|---|---|
| device | u16 | 设备号 |
| boot_seq | u32 | 启动序号 |
| epoch_ns | i64 | 本次启动 tick=0 对应的 Unix 纳秒 |
| tick_hz | u32 | 计数器频率，**必须为正** |

- 同设备**更大的 boot_seq** 开启新的计时周期：布局集合与 tick 状态重置；
  历史周期的样本与布局保留在库中，**不会被重新解释**；
- 同 boot_seq 且参数（epoch_ns、tick_hz）完全相同 → 重复，忽略；
  参数不同 → `boot_conflict` 拒收；更小的 boot_seq → `stale_boot` 拒收；
- `tick_hz = 0` 或 payload 长度不符 → `invalid_boot` 拒收。

### kind=1 布局声明（5 + 19×channel_count 字节）

头部：`device:u16, layout:u16, channel_count:u8`，随后按顺序
`channel_count` 个通道描述，每个 19 字节：

| 字段 | 类型 | 含义 |
|---|---|---|
| channel | u8 | 通道号 |
| raw_type | u8 | 1=int16，2=int32，3=float32 |
| gain | f64 | 校准增益 |
| bias | f64 | 校准偏置 |
| unit | u8 | 1=毫摄氏度，2=Pa，3=毫米每秒 |

- 物理量 = `raw*gain + bias`（声明单位下），再换算到最终单位：
  毫摄氏度→**°C**、Pa→**kPa**、毫米每秒→**m/s**（均 ×10⁻³）；
- gain/bias 非有限（NaN/Inf）、通道号重复、未知 raw_type/unit、长度不符
  → `invalid_layout` 拒收，声明**不生效**；
- 布局隶属于该设备**当前启动周期**。同周期同 layout 的**完全相同**声明
  → 重复，忽略；**不同**声明 → `layout_conflict` 拒收，既有解释不被悄悄更换；
- 设备未启动 → `unknown_device` 拒收。

### kind=2 测量包（8 + Σ通道字节）

头部：`device:u16, layout:u16, tick:u32`，随后按**声明顺序**排列各通道
原始值（int16/int32/float32）。长度必须与布局要求**恰好吻合**。

- 设备未启动 → `unknown_device`；当前周期无此布局 → `unknown_layout`；
  长度不符 → `bad_payload_length`；任一原始值为 NaN/Inf 或换算结果非有限
  → `nonfinite_value`：**整包拒收**，不接受部分通道；
- 拒收包**不推进**设备的 tick 状态。

## 3. tick 展开与时刻

计数器为 u32，会从 2³²−1 回绕到 0。以同设备当前周期**上一次被接纳**的
tick 为基准，记模 2³² 前进差 `d = (tick − last) mod 2³²`：

| d | 处置 |
|---|---|
| `d = 0` | 重传判定：布局与原始值完全一致 → 重复，不重复入库；否则 `tick_conflict` 拒收 |
| `0 < d < 2³¹` | 向前展开：`cum_tick = last_cum + d`，接纳 |
| `d ≥ 2³¹` | `stale_tick` 拒收（旧包） |

- 周期内**首个样本**以其无符号 tick 为累计刻度（`cum_tick = tick`）；
- 时刻用整数计算：`ts_ns = epoch_ns + floor(cum_tick × 10⁹ / tick_hz)`，
  不经过浮点，纳秒不丢失；`ts_ns` 超出 int64 → `timestamp_overflow` 拒收。

## 4. 流式解码与损坏重同步

接入器接受**任意大小的读取块**（`--chunk-size`，最小可到 1），跨块半帧
保留在缓冲中，解码结果与分块方式无关。损坏后的重同步规则：

1. 在缓冲中查找帧头魔数，魔数之前为垃圾，静默丢弃；
2. 候选帧 kind 越界或 `payload_len > 4096` → 记 `invalid_kind` /
   `invalid_length`，从魔数后一字节继续逐字节扫描；
3. 完整帧到齐但 CRC 不符 → 记 `crc_mismatch`，同样逐字节继续；
4. 只有**长度合法且 CRC 正确**的候选才被当作帧整帧消费——payload 内的
   普通帧头字节（如 int32 原始值 `0x09E15CA7`）不会被误当边界；
5. 文件尾部不足一帧 → 保持待续，进度不推进；补齐后恰好产出应有的那一包。

## 5. 断点续传与一致性

- 每个源文件登记 `tele_files`（路径 → file_id）；`tele_ingest_state`
  记录 `consumed_offset` 与已消费前缀的 SHA-256；
- 一批输入读到末尾后文件仍可追加：再次接入从上次进度继续；
- **同一事务**提交：帧应用、设备/布局/计数器状态、样本入库、进度保存。
  进程崩溃 → DuckDB 回滚未提交事务 → 从上一已提交进度重读，不重不漏。
  `--commit-every N` 可让长文件每 N 帧提交一次进度；
- 已处理前缀被改写、文件被替换、或文件被截断到进度之前 → **明确拒绝
  续接**（退出码 3），不会把新文件的后半段接到旧设备周期上。

## 6. 库表结构

| 表 | 内容 |
|---|---|
| `tele_files` | 源文件身份（路径 → file_id） |
| `tele_ingest_state` | 续读进度：已消费偏移、前缀 SHA-256、文件大小 |
| `tele_device_state` | 当前周期：boot_seq、epoch_ns、tick_hz、上一被接纳 tick（原始/累计）、末包内容 |
| `tele_layouts` / `tele_layout_channels` | 布局声明，键为 (device_id, boot_seq, layout_id)，历史周期保留 |
| `tele_samples` | 测量样本（见下） |
| `tele_frames` | 每个 CRC 合法帧的处置台账（kind、frame_len、applied/duplicate/rejected + 原因） |
| `tele_rejections` | 拒收明细（含解码异常），带 file_id 与帧起始偏移 |
| `tele_samples_view` | `tele_samples` + `make_timestamp_ns(ts_ns)` 时刻列 |

`tele_samples` 列：`file_id, frame_offset, device_id, boot_seq, layout_id,
tick, cum_tick, ts_ns, ordinal, channel, raw_type, raw_value, value, unit`。
任何测量值都可沿 `(file_id, frame_offset)` 回到源文件的确切原字节
（`tele_frames.frame_len` 给出帧长，源文件该偏移起读取这么长即为原帧），
沿 `(device_id, boot_seq, layout_id, ordinal)` 回到产生它的通道声明。

## 7. 命令行

```bash
# 生成可复现样例包（种子可复现，字节级一致）
python3 -m telemetry.cli gen --out data/tele --scenario all --seed 1

# 接入（可反复运行；--chunk-size 复现极端分片；--commit-every 分批提交）
python3 -m telemetry.cli ingest --db outputs/telemetry.duckdb \
    --file data/tele/mixed.bin --chunk-size 65536

# 查询
python3 -m telemetry.cli samples --db outputs/telemetry.duckdb \
    --device 7 --channel 0 --start-ns 1700000000400000000 --end-ns 1700000001000000000
python3 -m telemetry.cli trace --db outputs/telemetry.duckdb --file-id 1 --offset 131
python3 -m telemetry.cli frames|rejections|state --db outputs/telemetry.duckdb
python3 -m telemetry.cli query --db outputs/telemetry.duckdb --sql "SELECT ..."

# 演示
python3 -m telemetry.cli crash-demo --workdir /tmp/crash   # SIGKILL + 重启续传
python3 -m telemetry.cli demo --workdir /tmp/tele_demo     # 全流程
```

研究人员常用 SQL：

```sql
-- 时间段 + 通道筛选（ts_ns 为 Unix 纳秒；make_timestamp_ns 转为时刻）
SELECT channel, ts_ns, make_timestamp_ns(ts_ns) AS ts, value, unit
FROM tele_samples
WHERE device_id = 7 AND channel IN (0, 2)
  AND ts_ns >= 1700000000400000000 AND ts_ns < 1700000001000000000
ORDER BY ts_ns;

-- 原始偏移 → 标准测量值
SELECT * FROM tele_samples
WHERE file_id = 1 AND frame_offset = 131 ORDER BY ordinal;
```

## 8. 可复现包生成器

`telemetry.generator` 以种子生成确定性场景字节流（同一 seed 字节级一致）：
`mixed`（int16/int32/float32 混合通道）、`two-devices`（双设备交错）、
`wrap`（tick 回绕与旧包）、`retransmit`（相同/冲突声明与重传）、
`corrupt`（垃圾 + CRC 损坏 + payload 内嵌帧头）、`tail`（末帧缺尾，
分两个文件便于演示补齐）、`crash`（大流量双设备，供崩溃恢复压测）。

## 9. 核验体系（tests/test_telemetry.py）

测试的预期字节与期望值**独立手工编写**（struct.pack + zlib.crc32 +
手算数值），不经过被测的生成器/协议实现：

1. 混合整数/浮点通道的单位换算与纳秒时刻；
2. 两台设备交错的独立计时周期；
3. tick 回绕接纳、半圈边界（2³¹−1 接纳 / 2³¹ 拒收）、旧包拒收；
4. 相同/冲突布局重声明、相同/冲突测量包重传；
5. 非法启动/布局/负载（hz=0、重复通道、NaN 校准、NaN/Inf 原始值、长度不符）；
6. CRC 损坏、垃圾、非法 kind/长度后的逐字节重同步，payload 内嵌帧头不误判；
7. 分块无关性（块长 1/2/3/7/64/4096/1M 结果一致）与尾帧补齐；
8. 崩溃恢复：子进程在提交进度附近被 SIGKILL，重启续传后与干净参照库
   逐行一致、无重复样本、计数器状态一致；
9. 前缀改写/文件替换/截断到进度之前 → 明确拒绝续接；
10. 原始偏移回溯查询与时间段/通道筛选查询；
11. 生成器种子可复现性。
