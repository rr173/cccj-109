"""文件接入器：身份校验、任意块长读取、断点续读、进度与数据同一事务提交。

续读与防错
----------
- 每个源文件在 ``tele_files`` 登记身份，在 ``tele_ingest_state`` 记录
  ``consumed_offset``（已消费字节数）与 ``prefix_sha256``（已消费前缀的 SHA-256）；
- 再次接入同一文件时，先重读并校验已消费前缀：前缀被改写、文件被截断到
  进度之前、或整个文件被替换 → 明确拒绝（``IngestError``），绝不把新文件的
  后半段接到旧设备周期上；
- 文件尾部不足一帧的字节保持待续：进度只推进到解码器实际消费处，剩余字节
  下次重读，补齐后恰好产出应有的那一包；
- 一批之内：帧应用、设备/布局/计数器状态推进、样本入库、进度保存全部在
  **同一个 DuckDB 事务**中提交；进程崩溃 → 事务回滚 → 从上一已提交进度
  重读，结果不重不漏。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from . import store as store_mod
from .decoder import Anomaly, Frame, StreamDecoder

DEFAULT_CHUNK_SIZE = 65536


class IngestError(RuntimeError):
    """续读前提被破坏（前缀改写 / 文件替换 / 截断到进度之前）。"""


@dataclass
class IngestStats:
    file_id: int = 0
    path: str = ""
    start_offset: int = 0
    consumed_offset: int = 0
    pending_bytes: int = 0
    frames_applied: int = 0
    frames_duplicate: int = 0
    frames_rejected: int = 0
    anomalies: int = 0
    samples_added: int = 0
    notes: list = field(default_factory=list)


class Ingester:
    def __init__(self, db_path: str | Path) -> None:
        self.con = store_mod.connect(db_path)
        self.store = store_mod.TelemetryStore(self.con)

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "Ingester":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def ingest_file(self, path: str | Path,
                    chunk_size: int = DEFAULT_CHUNK_SIZE,
                    commit_every: int = 0,
                    progress: bool = False) -> IngestStats:
        """把文件自上次进度起的新增部分解码入库。返回统计。

        ``commit_every``：每处理多少帧提交一次（进度与数据同事务）；
        0 表示整批一次提交。崩溃后从最后一个已提交进度续读。
        ``progress``：每次中间提交后向 stdout 打印一行 ``PROGRESS …``
        （供崩溃演示/测试的父进程观察提交点）。
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须为正")
        path = Path(path)
        abspath = str(path.resolve())
        size = path.stat().st_size
        stats = IngestStats(path=abspath)

        self.store.begin()
        try:
            file_id, consumed, prefix_sha = self.store.register_or_lookup_file(
                abspath, size)
            stats.file_id = file_id
            stats.start_offset = consumed
            if size < consumed:
                raise IngestError(
                    f"文件长度 {size} 小于已处理进度 {consumed}："
                    f"文件被截断或替换，拒绝续接")
            decoder = StreamDecoder(base=consumed)
            hasher = hashlib.sha256()
            frames_since_commit = 0
            with open(path, "rb") as fh:
                # 1) 校验已消费前缀（逐块重读哈希）
                remaining = consumed
                while remaining > 0:
                    blk = fh.read(min(chunk_size, remaining))
                    if not blk:
                        raise IngestError(
                            f"文件在已处理进度 {consumed} 之前结束："
                            f"文件被截断或替换，拒绝续接")
                    hasher.update(blk)
                    remaining -= len(blk)
                if hasher.hexdigest() != prefix_sha:
                    raise IngestError(
                        f"已处理前缀校验不符：文件前 {consumed} 字节被改写或"
                        f"文件被替换，拒绝续接（历史周期不会被重新解释）")
                # 2) 逐块读新增数据；已消费字节并入前缀哈希
                unhashed = bytearray()      # 已读但解码器尚未消费的字节
                unhashed_base = consumed    # unhashed[0] 的绝对偏移
                while True:
                    chunk = fh.read(chunk_size)
                    if not chunk:
                        break
                    unhashed += chunk
                    for ev in decoder.feed(chunk):
                        if isinstance(ev, Frame):
                            res = self.store.apply_frame(file_id, ev)
                            frames_since_commit += 1
                            if res.disposition == store_mod.APPLIED:
                                stats.frames_applied += 1
                                stats.samples_added += res.n_samples
                            elif res.disposition == store_mod.DUPLICATE:
                                stats.frames_duplicate += 1
                            else:
                                stats.frames_rejected += 1
                        elif isinstance(ev, Anomaly):
                            self.store.record_anomaly(file_id, ev)
                            stats.anomalies += 1
                    take = decoder.base - unhashed_base
                    if take > 0:
                        hasher.update(bytes(unhashed[:take]))
                        del unhashed[:take]
                        unhashed_base = decoder.base
                    # 3) 分批提交：进度与已应用数据在同一事务落库
                    if commit_every and frames_since_commit >= commit_every:
                        self.store.update_ingest_state(
                            file_id, decoder.base, hasher.hexdigest(), size)
                        self.store.commit()
                        self.store.begin()
                        frames_since_commit = 0
                        if progress:
                            print(f"PROGRESS consumed={decoder.base}", flush=True)
            assert bytes(unhashed) == decoder.buffer  # 待续字节原样保留
            stats.consumed_offset = decoder.base
            stats.pending_bytes = decoder.pending_bytes
            self.store.update_ingest_state(file_id, decoder.base,
                                           hasher.hexdigest(), size)
            self.store.commit()
        except BaseException:
            self.store.rollback()
            raise
        return stats
