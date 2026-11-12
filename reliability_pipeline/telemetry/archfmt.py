"""折线压缩归档（TPLA，Telemetry PolyLine Archive）的字节格式定义。

本模块只放**格式规范常量**（魔数、结构体布局、CRC 约定），是构建器、
读取器与独立核验器共同引用的单一事实来源；算法与保证的论证见
``docs/archive_format.md``。多字节一律小端。

整体布局::

    文件头 64B（含 CRC）
    数据块 × N（每块：块头 40B + 线段部件 ×P + 逐点记录 ×M + CRC32）
    块索引（magic + 计数 + 定长项 ×N + CRC32）
    组目录（magic + 计数 + 变长项 ×G + CRC32）
    文件尾 36B（索引/目录偏移与长度 + CRC + 尾魔数）

设计要点
--------
- 逐点记录只含**精确时刻与定位键**（ts_ns、file_id、frame_offset、
  ordinal），共 22 字节，**不含物理值**；物理值只能由所属线段的
  24 字节模型参数（t0, y0, slope）与样本时刻算出；
- 块是自包含解码单元：块内每个线段部件都带完整模型参数，跨块段在
  每个块里各存一份参数（计入参数区字节）；
- 文件头、每个数据块、索引、目录、文件尾各自带 CRC32，截断或损坏
  都会被明确检出（``ArchiveError``），不会输出被称为完整的部分结果。
"""

from __future__ import annotations

import struct
import zlib

MAGIC = b"TPLA"
VERSION = 1

# 文件头：magic4, version u16, flags u16, device i32, channel i32,
#         epsilon f64, t_start i64, t_end i64, group_count u32,
#         block_count u32, created_unix i64, header_crc u32, 保留 4B
HEADER = struct.Struct("<4sHHiidqqIIqI4x")
HEADER_LEN = HEADER.size                     # 64（CRC 覆盖前 60 字节）

# 数据块头：magic u32, block_len u32, group_id u32, point_count u32,
#           part_count u32, flags u32, t_min i64, t_max i64
BLOCK_HEAD = struct.Struct("<IIIIIIqq")
BLOCK_MAGIC = 0x314B4C42                     # "BLK1"
BLOCK_HEAD_LEN = BLOCK_HEAD.size             # 40

# 线段部件：seg_seq u32, start_point u32, point_count u32, reserved u32,
#           t0 i64, y0 f64, slope f64
PART = struct.Struct("<IIIIqdd")
PART_LEN = PART.size                         # 40
PARAM_BYTES_PER_PART = 24                    # t0 + y0 + slope（模型参数区）

# 逐点记录：ts_ns i64, file_id i32, frame_offset i64, ordinal u16
POINT = struct.Struct("<qiqH")
POINT_LEN = POINT.size                       # 22

# 块索引项：group_id u32, t_min i64, t_max i64, file_off u64, block_len u32
INDEX_ENTRY = struct.Struct("<I4xqqQI4x")
INDEX_ENTRY_LEN = INDEX_ENTRY.size           # 40
INDEX_HEAD = struct.Struct("<II")            # magic + count
INDEX_MAGIC = 0x31584449                     # "IDX1"

# 组目录项：固定头 + unit 字节 + 统计尾
DIR_HEAD = struct.Struct("<II")              # magic + count
DIR_MAGIC = 0x31505247                       # "GRP1"
DIR_ENTRY_FIXED = struct.Struct("<IqiHH")    # gid, boot_seq i64, layout_id i32,
                                             # unit_len u16, flags u16
DIR_ENTRY_TAIL = struct.Struct("<QQQII")     # points, segs, param_bytes u64,
                                             # block_start u32, block_count u32
MAX_UNIT_LEN = 255

# 文件尾：index_off u64, index_len u32, dir_off u64, dir_len u32,
#         footer_crc u32, tail_magic 8B
FOOTER = struct.Struct("<QIQII8s")
FOOTER_LEN = FOOTER.size                     # 36（CRC 覆盖前 24 字节）
TAIL_MAGIC = b"\x00TPLAEND"


class ArchiveError(Exception):
    """归档文件结构不合法、被截断或已损坏。"""


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF
