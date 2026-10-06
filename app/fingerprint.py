"""幂等写入的内容指纹：规范化（canonicalization）与哈希生成（纯标准库）。

本模块集中定义各幂等写入路径的**内容指纹**生成逻辑，供 services 层在
"按单号/版本键查既有记录 → 比对指纹 → 同内容幂等返回 / 异内容冲突"的
统一模式下使用。覆盖的写入路径：

- 事件版本（单条 / 批量 / 预演-确认共用）：``event_fingerprint``
- 现场人工裁定：``ruling_fingerprint``
- 费率发布（整表规范化落库，版本号服务端分配）：``canonical_rate_lines``
- 停机损失按批次分摊：``allocation_fingerprint``
- 按设备期间停机损失结账：``closing_fingerprint``
- 计划保养单：``maintenance_plan_fingerprint``

统一的规范化规则（相同业务内容必须稳定命中原记录）：

1. **等价时区**：所有时间戳先归一为 UTC 的 ISO 串再参与指纹，
   同一时刻的 ``+08:00`` 与对应 ``Z`` 表示判定为同一内容；
2. **允许换序的列表**：顺序无业务含义的列表先按确定性规则排序——
   账 id 去重升序、批次按批次号升序、保养窗口按（起点, 终点）升序、
   费率行按（起点, 原因, 终点）升序；顺序有业务含义的列表（如批量
   补传的事件版本链）不参与排序；
3. **省略默认值**：可选字段（如说明 note）以默认值参与指纹，
   省略与显式给默认值（``""``）判定为同一内容；
4. **冲突保留**：会改变业务含义的字段（起止、原因、金额、单号作用域、
   截止序号、费率版本、批次窗口、账引用等）全部参与指纹，任何差异都
   产生不同指纹，从而保留 409 冲突；
5. **不放宽校验**：指纹只做归一化比较，字段校验（带时区、整分钟、
   区间方向、枚举值等）仍由 schema/服务层先行完成；规范化绝不把非法
   输入"修正"为合法，也不跨设备/班次合并编号作用域。

兼容性：指纹算法与既有库中已固化的 ``content_hash`` **逐字节兼容**
（事件/裁定为竖线分隔串，其余为键排序紧凑 JSON），升级后旧记录的
同内容重试仍稳定命中。变更任何格式都属于破坏性变更，禁止。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Iterable, Protocol, TypeVar

__all__ = [
    "utc_iso",
    "event_fingerprint",
    "ruling_fingerprint",
    "allocation_fingerprint",
    "closing_fingerprint",
    "maintenance_plan_fingerprint",
    "canonical_rate_lines",
]


def utc_iso(dt: datetime) -> str:
    """归一为 UTC 的 ISO 串：等价时区表示（如 +08:00 与对应 Z）得到同一串。

    输入必须是带时区的时间（schema 已强制）；本函数不做字段校验，
    只负责时区归一，绝不截断或取整——秒/微秒差异会保留在串中，
    由上游校验拒绝，指纹层面不抹平任何时间差异。
    """
    return dt.astimezone(timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _canonical_json(payload) -> str:
    """确定性 JSON：键排序、无空白分隔、非 ASCII 不转义。

    与既有库中分摊/结账/保养计划已固化的 content_hash 逐字节兼容。
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _default_note(note: str | None) -> str:
    """可选说明字段的默认值归一：省略（None）与显式空串等价。"""
    return note or ""


# ---------------------------------------------------------------- 事件版本


def event_fingerprint(start: datetime, end: datetime, reason: str) -> str:
    """事件版本内容指纹：``sha256(start_utc|end_utc|reason)``。

    业务键（设备, 来源, 版本）由版本链唯一确定，指纹只覆盖内容字段；
    起止归一 UTC，等价时区的同内容重试稳定命中，异内容保留冲突。
    """
    return _sha256(f"{utc_iso(start)}|{utc_iso(end)}|{reason}")


# ---------------------------------------------------------------- 现场人工裁定


def ruling_fingerprint(
    start: datetime, end: datetime, reason: str, note: str | None
) -> str:
    """裁定内容指纹：``sha256(start_utc|end_utc|reason|note)``。

    业务键（班次, 裁定单号）唯一确定记录；note 省略与空串等价。
    指纹不含班次/单号——同一单号在不同班次是不同记录（不跨班次复用）。
    """
    return _sha256(
        f"{utc_iso(start)}|{utc_iso(end)}|{reason}|{_default_note(note)}"
    )


# ---------------------------------------------------------------- 费率发布


class _RateLineLike(Protocol):
    """费率行的结构要求（pricing.RateLine 满足）：原因、起止、每分钟金额。"""

    reason: str
    start: datetime
    end: datetime


_RateLineT = TypeVar("_RateLineT", bound=_RateLineLike)


def canonical_rate_lines(lines: Iterable[_RateLineT]) -> list[_RateLineT]:
    """费率表的规范化顺序：按（起点, 原因, 终点）升序。

    费率发布整批原子落库、版本号由服务端分配，其"内容指纹"即规范化后的
    整表：提交列表换序不影响落库内容（entry_no 按本顺序编号）；金额在
    pricing.parse_amount 中已归一为定点微元（"1" 与 "1.00" 同值），
    时间由调用方归一 UTC——等价提交的版本内容逐字节一致。
    """
    return sorted(lines, key=lambda ln: (ln.start, ln.reason, ln.end))


# ---------------------------------------------------------------- 停机损失按批次分摊


def allocation_fingerprint(
    accounting_ids: Iterable[int],
    batches: Iterable[tuple[str, datetime, datetime]],
) -> str:
    """分摊单提交内容指纹（与列表顺序无关）。

    规范化：账 id 去重升序、批次按批次号升序、窗口端点归一 UTC。
    设备未单独入指纹：账 id 全局唯一且各自唯一确定所属设备，
    同一组账 id 已蕴含同一设备，不会因此跨设备误判同内容。
    """
    canonical_accounts = sorted(set(accounting_ids))
    canonical_batches = sorted(
        ((batch_no, utc_iso(start), utc_iso(end)) for batch_no, start, end in batches),
        key=lambda b: b[0],
    )
    payload = {
        "accounting_ids": canonical_accounts,
        "batches": [
            {"batch_no": batch_no, "start": start, "end": end}
            for batch_no, start, end in canonical_batches
        ],
    }
    return _sha256(_canonical_json(payload))


# ---------------------------------------------------------------- 按设备期间停机损失结账


def closing_fingerprint(
    device_id: str,
    start: datetime,
    end: datetime,
    seq: int,
    rate_version: int,
) -> str:
    """结账输入指纹：设备 + 期间（UTC）+ 截止序号 + 费率版本。

    设备号参与指纹：结账单号虽全局唯一，异设备同单号不同输入必须冲突，
    不得跨设备复用编号。
    """
    payload = {
        "device_id": device_id,
        "start": utc_iso(start),
        "end": utc_iso(end),
        "seq": int(seq),
        "rate_version": int(rate_version),
    }
    return _sha256(_canonical_json(payload))


# ---------------------------------------------------------------- 计划保养单


def maintenance_plan_fingerprint(
    device_id: str,
    start: datetime,
    end: datetime,
    windows: Iterable[tuple[datetime, datetime]],
) -> str:
    """保养计划内容指纹：设备 + 期间（UTC）+ 规范化窗口列表。

    窗口顺序无业务含义：按（起点, 终点）升序后参与指纹，窗口换序、
    等价时区的同内容重试稳定命中；期间或窗口的实际差异保留冲突。
    """
    canonical_windows = sorted((utc_iso(s), utc_iso(e)) for s, e in windows)
    payload = {
        "device_id": device_id,
        "start": utc_iso(start),
        "end": utc_iso(end),
        "windows": [{"start": s, "end": e} for s, e in canonical_windows],
    }
    return _sha256(_canonical_json(payload))
