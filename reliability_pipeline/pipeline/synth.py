"""合成观察日志生成器（种子可复现）。

生成的日志刻意混入各种"脏"情形，用于压力测试管线政策：

- 延迟入组（左截断）、偶发入组早于投产（设备拒收）
- 故障 / 退役 / 失访 / 无事件（观察截止删失）
- 终止事件偶发落在入组前（设备拒收）或观察窗口外（窗口外忽略）
- 撤销：正常撤销、撤销的撤销、循环撤销、目标不存在的撤销
- 非法日期、未知事件类型、未知设备、重复 event_id
- 收到时间滞后于事件日期 0..25 天 → 部分事实在知识截点后才可见
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path
from typing import List, Tuple

BASE_DATE = date(2023, 1, 1)
OBS_END_OFFSET = 75          # observation_end = BASE + 75 天
CUTOFF_LAG = 10              # knowledge_cutoff = observation_end + 10 天
MACHINE_TYPES = ("A", "B", "C")


def generate(
    seed: int,
    n_devices: int,
    out_dir: str | Path | None = None,
) -> Tuple[List[dict], List[dict], date, date]:
    """返回 (devices_rows, events_rows, observation_end, knowledge_cutoff)。"""
    rng = random.Random(seed)
    obs_end = BASE_DATE + timedelta(days=OBS_END_OFFSET)
    cutoff = obs_end + timedelta(days=CUTOFF_LAG)

    devices: List[dict] = []
    events: List[dict] = []
    eid_seq = [0]

    def next_eid() -> str:
        eid_seq[0] += 1
        return f"S{eid_seq[0]:05d}"

    def recv(d: date) -> date:
        return d + timedelta(days=rng.randint(0, 25))

    device_ids = []
    for i in range(n_devices):
        did = f"R{i:04d}"
        device_ids.append(did)
        commission = BASE_DATE + timedelta(days=rng.randint(0, 40))
        if rng.random() < 0.03:  # 入组早于投产 → 设备拒收
            enroll = commission - timedelta(days=rng.randint(1, 5))
        else:
            enroll = commission + timedelta(days=rng.randint(0, 20))
        mtype = rng.choice(MACHINE_TYPES)
        devices.append({
            "device_id": did, "machine_type": mtype,
            "commission_date": commission.isoformat(), "enroll_date": enroll.isoformat(),
        })

        # ---- 终止事件
        r = rng.random()
        terminal_events = []  # 可能多个（同日多事件 / 窗口外事件）
        if r < 0.40:
            kind = "failure"
        elif r < 0.58:
            kind = "retirement"
        elif r < 0.68:
            kind = "loss_to_followup"
        else:
            kind = None
        if kind is not None:
            day = commission + timedelta(days=rng.randint(1, 100))
            if rng.random() < 0.06:  # 入组前终止 → 设备拒收
                day = commission + timedelta(days=rng.randint(0, max(0, (enroll - commission).days)))
                if day >= enroll:
                    day = enroll - timedelta(days=1) if enroll > commission else commission
            e = {
                "event_id": next_eid(), "device_id": did, "event_type": kind,
                "event_date": day.isoformat(), "received_at": recv(day).isoformat(),
                "voids_event_id": "", "note": "",
            }
            events.append(e)
            terminal_events.append(e)
            # 同日第二终止事件（触发固定优先级）
            if rng.random() < 0.05:
                other = rng.choice([k for k in ("failure", "retirement", "loss_to_followup")
                                    if k != kind])
                events.append({
                    "event_id": next_eid(), "device_id": did, "event_type": other,
                    "event_date": day.isoformat(), "received_at": recv(day).isoformat(),
                    "voids_event_id": "", "note": "同日多事件",
                })

        # ---- 撤销类噪声
        if terminal_events and rng.random() < 0.10:  # 正常撤销
            tgt = rng.choice(terminal_events)
            vday = date.fromisoformat(tgt["event_date"]) + timedelta(days=rng.randint(1, 30))
            events.append({
                "event_id": next_eid(), "device_id": did, "event_type": "void",
                "event_date": vday.isoformat(), "received_at": recv(vday).isoformat(),
                "voids_event_id": tgt["event_id"], "note": "撤销故障记录",
            })
        if rng.random() < 0.02:  # 循环撤销对
            d0 = commission + timedelta(days=rng.randint(1, 30))
            a, b = next_eid(), next_eid()
            events.append({
                "event_id": a, "device_id": did, "event_type": "void",
                "event_date": d0.isoformat(), "received_at": recv(d0).isoformat(),
                "voids_event_id": b, "note": "循环撤销A",
            })
            events.append({
                "event_id": b, "device_id": did, "event_type": "void",
                "event_date": d0.isoformat(), "received_at": recv(d0).isoformat(),
                "voids_event_id": a, "note": "循环撤销B",
            })
        if rng.random() < 0.02:  # 目标不存在的撤销
            d0 = commission + timedelta(days=rng.randint(1, 30))
            events.append({
                "event_id": next_eid(), "device_id": did, "event_type": "void",
                "event_date": d0.isoformat(), "received_at": recv(d0).isoformat(),
                "voids_event_id": "S99999", "note": "目标不存在",
            })

    # ---- 全局噪声
    if rng.random() < 0.7:  # 非法日期事件
        did = rng.choice(device_ids)
        events.append({
            "event_id": next_eid(), "device_id": did, "event_type": "failure",
            "event_date": "2023-13-40", "received_at": "2023-06-01",
            "voids_event_id": "", "note": "非法日期",
        })
    if rng.random() < 0.5:  # 未知设备
        events.append({
            "event_id": next_eid(), "device_id": "R9999", "event_type": "failure",
            "event_date": "2023-03-01", "received_at": "2023-03-02",
            "voids_event_id": "", "note": "未知设备",
        })
    if rng.random() < 0.4 and events:  # 重复 event_id（禁止覆写 → 拒收后者）
        dup = dict(rng.choice(events))
        dup["note"] = "重复event_id"
        events.append(dup)
    if rng.random() < 0.3:  # 未知事件类型
        did = rng.choice(device_ids)
        events.append({
            "event_id": next_eid(), "device_id": did, "event_type": "explosion",
            "event_date": "2023-04-01", "received_at": "2023-04-02",
            "voids_event_id": "", "note": "未知类型",
        })

    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        with open(out / "devices.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["device_id", "machine_type",
                                               "commission_date", "enroll_date"])
            w.writeheader()
            w.writerows(devices)
        with open(out / "events.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["event_id", "device_id", "event_type",
                                               "event_date", "received_at",
                                               "voids_event_id", "note"])
            w.writeheader()
            w.writerows(events)
    return devices, events, obs_end, cutoff
