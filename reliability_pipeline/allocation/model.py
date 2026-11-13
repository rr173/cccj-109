"""规范化：把已校验的输入变成排序固定、可复现的优化问题。

规范排序
--------
- 层（stratum）与 cluster 都按 Python 字符串码点顺序排序（UTF-8 文本的确定
  顺序；不做 locale 或大小写折叠）；
- 设备按 ``device_id`` 文本排序；
- CSV 行序与 JSON 对象键序**不参与**排序。因此同一内容只调换 CSV 行序时，
  规范化问题、``Z``、组序以及 rank ↔ 方案对应完全不变。

cluster 的向量贡献
------------------
设规范层序为 ``s_0..s_{k-1}``，cluster ``c`` 在各层的设备台数为
``v_c = (n_{c,0}, ..., n_{c,k-1})``。把 ``c`` 分到 A 时从剩余配额扣除整个
``v_c``；分到 B 时扣除零向量。设备不会被拆开，因为每一步的决策单位是 cluster。

锁定
----
设计文件中锁定的 cluster 方案固定，**先于 DP** 计入配额推理：
锁定为 A 的贡献从各层配额中扣除（``residual = target_A - 锁定A贡献``）；
锁定为 B 的没有贡献。若扣除后某层为负（锁定本身已超额）或剩余需求超过
未锁定设备数，``reduce_locks`` 给出逐层说明，不进入 DP。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from .inputs import AllocationInputError, Design, LockSpec

A = "A"
B = "B"


@dataclass(frozen=True)
class Device:
    device_id: str
    stratum: str
    cluster_id: str


@dataclass
class Problem:
    devices: Tuple[Device, ...]                 # 按 device_id 排序
    strata: Tuple[str, ...]                     # 规范层序
    clusters: Tuple[str, ...]                   # 规范 cluster 序
    stratum_index: Dict[str, int]
    cluster_index: Dict[str, int]
    # membership[c] = ((stratum_index, count), ...)，按层索引升序、count>0
    membership: Tuple[Tuple[Tuple[int, int], ...], ...]
    stratum_sizes: Tuple[int, ...]
    targets: Tuple[int, ...]
    # 锁定按 cluster 规范序：((cluster_index, "A"|"B"), ...)
    locks: Tuple[Tuple[int, str], ...]
    lock_specs: Tuple[LockSpec, ...]
    lock_by_cluster: Dict[int, str]
    design_strata: Tuple[str, ...]

    def devices_of_cluster(self, c: int) -> List[Device]:
        cid = self.clusters[c]
        return [d for d in self.devices if d.cluster_id == cid]

    def membership_dense(self, c: int, k: int | None = None) -> Tuple[int, ...]:
        k = len(self.strata) if k is None else k
        v = [0] * k
        for si, n in self.membership[c]:
            v[si] = n
        return tuple(v)


def prepare(rows: Sequence, design: Design) -> Problem:
    """校验「设计 vs 设备名单」的一致性，返回规范化问题。

    设备行自身的问题已在 ``inputs.read_device_csv`` 拒绝；这里只报设计层面的
    未知层、缺配额、配额越界与未知锁定 cluster。
    """
    problems: List[str] = []
    devices = tuple(sorted((Device(r.device_id, r.stratum, r.cluster_id)
                            for r in rows), key=lambda d: d.device_id))
    strata = tuple(sorted({d.stratum for d in devices}))
    clusters = tuple(sorted({d.cluster_id for d in devices}))
    stratum_index = {s: i for i, s in enumerate(strata)}
    cluster_index = {c: i for i, c in enumerate(clusters)}

    stratum_sizes = [0] * len(strata)
    for d in devices:
        stratum_sizes[stratum_index[d.stratum]] += 1

    # 未知层：设计给了配额，名单里没有该层
    for s in sorted(set(design.targets) - set(strata)):
        problems.append(
            f"设计文件 target_A[{s!r}]: 未知层——设备名单中没有任何设备的 "
            "stratum 等于该文本")
    # 缺配额：名单中的层设计未给 target_A
    for s in strata:
        if s not in design.targets:
            problems.append(
                f"设计文件缺少层 {s!r} 的 target_A（该层在设备名单中出现，"
                f"共 {stratum_sizes[stratum_index[s]]} 台设备；"
                "每个实际出现的层都必须指定配额）")
    targets = tuple(design.targets.get(s, 0) for s in strata)
    for s, t in zip(strata, targets):
        size = stratum_sizes[stratum_index[s]]
        if s in design.targets and not (0 <= t <= size):
            problems.append(
                f"设计文件 target_A[{s!r}]={t} 越界：该层只有 {size} 台设备，"
                f"配额必须在 [0, {size}] 内")

    lock_by_cluster: Dict[int, str] = {}
    lock_specs: List[LockSpec] = []
    for spec in design.locks:
        if spec.cluster_id not in cluster_index:
            problems.append(
                f"设计文件 {spec.position}: 锁定项引用未知 cluster "
                f"{spec.cluster_id!r}（设备名单中没有该 cluster_id）")
            continue
        ci = cluster_index[spec.cluster_id]
        lock_by_cluster[ci] = spec.arm
        lock_specs.append(spec)
    if problems:
        raise AllocationInputError(problems)

    membership: List[Tuple[Tuple[int, int], ...]] = []
    for c in clusters:
        counts: Dict[int, int] = {}
        for d in devices:
            if d.cluster_id == c:
                si = stratum_index[d.stratum]
                counts[si] = counts.get(si, 0) + 1
        membership.append(tuple(sorted(counts.items())))

    locks = tuple(sorted(lock_by_cluster.items()))
    lock_specs.sort(key=lambda sp: (cluster_index[sp.cluster_id], sp.position))

    return Problem(
        devices=devices, strata=strata, clusters=clusters,
        stratum_index=stratum_index, cluster_index=cluster_index,
        membership=tuple(membership),
        stratum_sizes=tuple(stratum_sizes), targets=targets,
        locks=locks, lock_specs=tuple(lock_specs),
        lock_by_cluster=lock_by_cluster,
        design_strata=tuple(sorted(design.targets)))


@dataclass
class LockedReduction:
    remaining_clusters: List[int]       # 未锁定 cluster 的规范索引，升序
    lock_contrib: Dict[int, int]        # 层索引 → 锁定为 A 的贡献
    lock_A_clusters: List[int]
    lock_B_clusters: List[int]
    residual: List[int]                 # 层索引 → target_A − 锁定 A 贡献
    residual_devices: List[int]         # 层索引 → 未锁定设备数
    failures: List[str]                 # 锁定合并即已无解的逐层说明


def reduce_locks(prob: Problem) -> LockedReduction:
    """把锁定项先纳入配额推理；返回剩余配额与逐层可行性说明（不抛异常）。"""
    k = len(prob.strata)
    lock_contrib = [0] * k
    lock_A: List[int] = []
    lock_B: List[int] = []
    for ci, arm in prob.locks:
        if arm == A:
            lock_A.append(ci)
            for si, n in prob.membership[ci]:
                lock_contrib[si] += n
        else:
            lock_B.append(ci)
    residual = [prob.targets[i] - lock_contrib[i] for i in range(k)]
    freed = [0] * k
    for ci in range(len(prob.clusters)):
        if ci in prob.lock_by_cluster:
            continue
        for si, n in prob.membership[ci]:
            freed[si] += n

    failures: List[str] = []
    for i, s in enumerate(prob.strata):
        if residual[i] < 0:
            parts = []
            for ci in lock_A:
                n = next((n for si, n in prob.membership[ci] if si == i), 0)
                if n:
                    parts.append(f"{prob.clusters[ci]!r} 贡献 {n} 台")
            failures.append(
                f"层 {s!r}: 锁定到 A 的 cluster 已贡献 {lock_contrib[i]} 台，"
                f"超过 target_A={prob.targets[i]}（超出 {-residual[i]} 台）"
                + (f"（{'; '.join(parts)}）" if parts else ""))
        elif residual[i] > freed[i]:
            failures.append(
                f"层 {s!r}: 扣除锁定 A 贡献后仍需 {residual[i]} 台进入 A，"
                f"但该层未锁定设备只有 {freed[i]} 台")

    return LockedReduction(
        remaining_clusters=[ci for ci in range(len(prob.clusters))
                            if ci not in prob.lock_by_cluster],
        lock_contrib=dict(enumerate(lock_contrib)),
        lock_A_clusters=lock_A, lock_B_clusters=lock_B,
        residual=residual, residual_devices=freed, failures=failures)
