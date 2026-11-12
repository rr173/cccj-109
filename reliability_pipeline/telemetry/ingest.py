"""文件接入器：身份校验、任意块长读取、断点续读、进度与数据同一事务提交。

续读与防错
----------
- 每个源文件在 ``tele_files`` 登记身份（``file_id`` + 上次实际读到的对象
  身份 ``identity`` = fd 的 (dev, ino)），在 ``tele_ingest_state`` 记录
  ``consumed_offset``（已消费字节数）与 ``prefix_sha256``（已消费前缀的
  SHA-256；即对 ``[0, consumed_offset)`` 的字节直接求 SHA-256）；
- **身份来自实际打开用于读取的对象**：先 ``os.open`` 一次，随后所有判定
  （大小、前缀哈希、登记/续读、读取新增数据）都针对同一个 fd 上的文件
  对象（``os.fstat``），不再「先检查路径、随后再次按路径打开」；
- 检查与读取之间若路径被 ``os.replace`` 替换，本次明确只完成已经打开的
  原对象：新数据只可能来自该对象，身份/进度也绑定到它；随后再按被替换的
  路径运行会因身份不符被拒绝（也可以选择拒绝本次操作，二者都不会把新
  对象的数据记到旧来源名下）；
- 正常增长（同一对象、同一路径）继续读；**同一对象**改了登记路径也继续
  读（身份相同，``tele_files.path`` 随之更新）；**另一个对象占用旧路径**
  则明确拒绝（``IngestError``），整批事务回滚：不新增样本、不写拒收台账、
  不推进旧进度；
- 文件尾部不足一帧的字节保持待续：进度只推进到解码器实际消费处，剩余
  字节下次重读，补齐后恰好产出应有的那一包；
- 一批之内：帧应用、设备/布局/计数器状态推进、样本入库、来源身份与进度
  保存全部在 **同一个 DuckDB 事务**中提交；进程崩溃 → 事务回滚 → 从上一
  已提交进度重读，结果不重不漏。
- 旧库兼容：``tele_files.identity`` 为 NULL（旧库只有路径 + 前缀哈希）时，
  无法证明历史来源身份，**保守阻止自动续接**，不把当下路径指向的文件
  认领为历史来源；旧记录与样本原样保留，可用前缀哈希审计。新建库没有
  这个问题：登记时即写入身份，重复运行可证明同一对象。
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_mod
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator, Optional

from . import store as store_mod
from .decoder import Anomaly, Frame, StreamDecoder
from .errors import IngestError

DEFAULT_CHUNK_SIZE = 65536


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
    prefix_sha256: str = ""
    notes: list = field(default_factory=list)


@dataclass(frozen=True)
class SourceCheck:
    """对实际打开对象做过的检查结果（供测试/审计观察）。"""

    file_id: int
    identity: str
    size: int
    consumed_offset: int
    path_updated: bool = False


class Ingester:
    def __init__(self, db_path: str | Path) -> None:
        self.con = store_mod.connect(db_path)
        self.store = store_mod.TelemetryStore(self.con)
        self._last_source_check: Optional[SourceCheck] = None

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "Ingester":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def last_source_check(self) -> Optional[SourceCheck]:
        return self._last_source_check

    # ---- 身份与进度判定 ------------------------------------------------

    def _bind_source(self, path: str, fstat: os.stat_result,
                     size: int) -> store_mod.FileSource:
        """在**已打开对象**的 fstat 上判定来源身份；必要时登记。

        调用方必须在同一个事务里调用，保证「登记 + 后续写入」原子提交。
        判定顺序：先按登记路径，再按对象身份（同一文件改名），最后才登记
        新来源；身份不一致一律拒绝，绝不认领。
        """
        identity = store_mod.encode_identity(fstat.st_dev, fstat.st_ino)
        row = self.store.lookup_file(path)
        path_updated = False
        if row is None:
            # 路径没登记过：可能是同一对象被移动/改名。按对象身份找旧来源。
            row = self.store.lookup_file_by_identity(identity)
            if row is not None:
                self.store.update_file_path(row.file_id, path)
                path_updated = True
        if row is None:
            src = self.store.register_file(path, identity, size)
        elif row.identity is None:
            # 旧库行：只有路径 + 前缀校验，没有对象身份。
            if row.consumed_offset == 0:
                # 没有已处理前缀可继承：不涉及历史归属，补登记后照常接入。
                src = self.store.claim_unregistered(path, identity, size)
                path_updated = False          # 旧行沿用原登记路径
            else:
                # 已有进度却无法证明属于哪个对象：绝不把当下路径指向的
                # 文件直接认领为历史来源 → 保守阻止自动续接。
                raise IngestError(
                    f"来源 file_id={row.file_id}（{row.path}）缺少对象身份信息"
                    f"（旧库格式），无法证明已处理 {row.consumed_offset} 字节属于"
                    f"当前文件；保守阻止自动续接。旧记录与样本保持原样，可用"
                    f"前缀哈希审计（不认领、不清空）")
        elif row.identity != identity:
            raise IngestError(
                f"路径 {path} 已被另一个文件对象占用：登记的来源 file_id="
                f"{row.file_id} 身份为 {row.identity}，当前打开对象身份为 "
                f"{identity}；文件被替换，拒绝把新对象的数据记到旧来源名下")
        else:
            src = row
        if size < src.consumed_offset:
            raise IngestError(
                f"文件长度 {size} 小于已处理进度 {src.consumed_offset}："
                f"文件被截断或替换，拒绝续接")
        self._last_source_check = SourceCheck(
            src.file_id, identity, size, src.consumed_offset, path_updated)
        return src

    @staticmethod
    def _verify_prefix(fh: BinaryIO, consumed: int, chunk_size: int,
                       expected_sha: str) -> "hashlib._Hash":
        """重读已消费前缀并校验哈希（全部在已打开的对象上）。

        返回的 hasher 已包含 ``[0, consumed)`` 的全部字节，可继续累计新增
        字节，使保存的摘要始终是「已消费前缀的 SHA-256」。
        """
        hasher = hashlib.sha256()
        remaining = consumed
        while remaining > 0:
            blk = fh.read(min(chunk_size, remaining))
            if not blk:
                raise IngestError(
                    f"文件在已处理进度 {consumed} 之前结束："
                    f"文件被截断或替换，拒绝续接")
            hasher.update(blk)
            remaining -= len(blk)
        if hasher.hexdigest() != expected_sha:
            raise IngestError(
                f"已处理前缀校验不符：文件前 {consumed} 字节被改写或"
                f"文件被替换，拒绝续接（历史周期不会被重新解释）")
        return hasher

    # ---- 主流程 --------------------------------------------------------

    def ingest_file(self, path: str | Path,
                    chunk_size: int = DEFAULT_CHUNK_SIZE,
                    commit_every: int = 0,
                    progress: bool = False,
                    after_open=None,
                    _crash_after_commit: int | None = None,
                    ) -> IngestStats:
        """把文件自上次进度起的新增部分解码入库。返回统计。

        ``commit_every``：每处理多少帧提交一次（进度与数据同事务）；
        0 表示整批一次提交。崩溃后从最后一个已提交进度续读。
        ``progress``：每次中间提交后向 stdout 打印一行 ``PROGRESS …``
        （供崩溃演示/测试的父进程观察提交点）。
        ``after_open``：对象打开且完成前缀校验后、读取新增数据之前调用的
        测试钩子（``fn(open_file)``），用于可控地复现「检查期间路径被替换」。
        ``_crash_after_commit``：测试用内建崩溃钩子——完成第 N 次进度提交后
        立即 ``os._exit``（不经过 DuckDB 清理，等价于被 SIGKILL）。
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须为正")
        abspath = str(Path(path).resolve())
        stats = IngestStats(path=abspath)

        try:
            fd = os.open(abspath, os.O_RDONLY)
        except FileNotFoundError as e:
            raise IngestError(f"源文件不存在：{abspath}") from e
        try:
            fst = os.fstat(fd)
            if not stat_mod.S_ISREG(fst.st_mode):
                raise IngestError(f"源文件不是普通文件：{abspath}")
            size = fst.st_size
            self.store.begin()
            try:
                src = self._bind_source(abspath, fst, size)
                stats.file_id = src.file_id
                stats.start_offset = src.consumed_offset
                with _fd_file(fd, fst) as fh:
                    hasher = self._verify_prefix(
                        fh, src.consumed_offset, chunk_size, src.prefix_sha256)
                    if after_open is not None:
                        after_open(fh)
                    stats = self._read_new(
                        fh, src, size, stats, chunk_size, commit_every,
                        progress, hasher, _crash_after_commit)
                stats.prefix_sha256 = hasher.hexdigest()
                self.store.update_source(src.file_id, src.identity,
                                         stats.consumed_offset,
                                         stats.prefix_sha256,
                                         os.fstat(fd).st_size)
                self.store.commit()
            except BaseException:
                self.store.rollback()
                raise
        finally:
            os.close(fd)
        return stats

    def _read_new(self, fh: BinaryIO, src: store_mod.FileSource, size: int,
                  stats: IngestStats, chunk_size: int, commit_every: int,
                  progress: bool, hasher: "hashlib._Hash",
                  crash_after_commit: int | None) -> IngestStats:
        identity = src.identity
        if identity is None:                       # _bind_source 已拦截，防御性
            raise IngestError("来源身份未知，拒绝续接（旧库）")
        decoder = StreamDecoder(base=src.consumed_offset)
        frames_since_commit = 0
        unhashed = bytearray()      # 已读但解码器尚未消费的字节
        unhashed_base = src.consumed_offset    # unhashed[0] 的绝对偏移
        n_commits = 0
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            unhashed += chunk
            for ev in decoder.feed(chunk):
                if isinstance(ev, Frame):
                    res = self.store.apply_frame(src.file_id, ev)
                    frames_since_commit += 1
                    if res.disposition == store_mod.APPLIED:
                        stats.frames_applied += 1
                        stats.samples_added += res.n_samples
                    elif res.disposition == store_mod.DUPLICATE:
                        stats.frames_duplicate += 1
                    else:
                        stats.frames_rejected += 1
                elif isinstance(ev, Anomaly):
                    self.store.record_anomaly(src.file_id, ev)
                    stats.anomalies += 1
            take = decoder.base - unhashed_base
            if take > 0:
                hasher.update(bytes(unhashed[:take]))
                del unhashed[:take]
                unhashed_base = decoder.base
            # 分批提交：进度、来源身份与已应用数据在同一事务落库
            if commit_every and frames_since_commit >= commit_every:
                n_commits += 1
                self.store.update_source(src.file_id, identity, decoder.base,
                                         hasher.hexdigest(),
                                         os.fstat(fh.fileno()).st_size)
                self.store.commit()
                frames_since_commit = 0
                if progress:
                    print(f"PROGRESS consumed={decoder.base}", flush=True)
                if crash_after_commit is not None and n_commits >= crash_after_commit:
                    os._exit(137)      # 模拟提交后立即被 SIGKILL
                self.store.begin()
        assert bytes(unhashed) == decoder.buffer  # 待续字节原样保留
        stats.consumed_offset = decoder.base
        stats.pending_bytes = decoder.pending_bytes
        return stats


@contextmanager
def _fd_file(fd: int, fst: os.stat_result) -> Iterator[BinaryIO]:
    """把已打开的 fd 包成二进制文件对象；不重新按路径打开。

    ``closefd=False``：fd 的生命周期由外层管理。封装后再对照一次 fstat，
    确认封装的就是通过校验的同一个对象。
    """
    f = open(fd, "rb", closefd=False)
    try:
        st2 = os.fstat(f.fileno())
        if (st2.st_dev, st2.st_ino) != (fst.st_dev, fst.st_ino):
            raise IngestError("打开的文件对象与校验对象不一致")
        yield f
    finally:
        f.close()
