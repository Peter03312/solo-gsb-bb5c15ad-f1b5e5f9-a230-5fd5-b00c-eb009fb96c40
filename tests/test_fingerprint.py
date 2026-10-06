"""幂等写入内容指纹的单元测试（仅依赖标准库）。

可用 pytest 运行，也可直接：python3 tests/test_fingerprint.py

覆盖统一的规范化规则（见 app/fingerprint.py 模块文档）：
- 等价时区：同一时刻的 +08:00 / Z / 其他偏移表示 → 同一指纹；
- 允许换序的列表：账 id、批次窗口、保养窗口、费率行换序 → 同一指纹/同一规范化顺序；
- 省略默认值：说明字段省略（None）与显式空串 → 同一指纹；
- 冲突保留：任何改变业务含义的字段差异 → 不同指纹；
- 不放宽校验：指纹不抹平秒级时间差异（整分钟校验在上游，不在指纹层）；
- 逐字节兼容：指纹输出与既有库已固化 content_hash 的历史算法完全一致
  （下列特征向量由重构前实现算出，变更即破坏既有记录的幂等命中）。
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.fingerprint import (  # noqa: E402
    allocation_fingerprint,
    canonical_rate_lines,
    closing_fingerprint,
    event_fingerprint,
    maintenance_plan_fingerprint,
    ruling_fingerprint,
    utc_iso,
)

UTC = timezone.utc
TZ8 = timezone(timedelta(hours=8))
TZ_N5 = timezone(timedelta(hours=-5))


def _ts(hh: int, mm: int = 0, tz: timezone = TZ8) -> datetime:
    return datetime(2026, 10, 1, hh, mm, tzinfo=tz)


# 同一时刻的三种等价表示：10:00+08:00 == 02:00Z == 21:00(-05:00，前一天)
SAME_INSTANT = [
    (_ts(10, tz=TZ8), _ts(11, tz=TZ8)),
    (_ts(2, tz=UTC), _ts(3, tz=UTC)),
    (datetime(2026, 9, 30, 21, 0, tzinfo=TZ_N5), datetime(2026, 9, 30, 22, 0, tzinfo=TZ_N5)),
]


# ---------------------------------------------------------------- 逐字节兼容特征向量
# 由重构前 services.py 的内联实现算出；任何格式变更都会破坏既有库中
# 已固化 content_hash 的幂等命中，这些向量必须保持不变。


def test_characterization_vectors_byte_compatible():
    s, e = _ts(10), _ts(11)  # 2026-10-01T02:00:00+00:00 .. 03:00:00+00:00
    assert (
        event_fingerprint(s, e, "maintenance")
        == "ad749fe74e930f55b9e2a1bcf8a6b45fe6503853e7f1703843d6a8d27104f31a"
    )
    assert (
        ruling_fingerprint(s, e, "pending", "传感器无记录，待质检复核")
        == "d3f14f12d6b3a2c171079c4e6ca1ca1043fe33821574eb12f0a729991c3d1e17"
    )
    assert (
        allocation_fingerprint(
            [3, 1, 2],
            [("B2", s, e), ("B1", e, _ts(12))],
        )
        == "48f7476f0735e2e5e5c2023475e7a892c44694412d7a2570cd92d8cdea1fc464"
    )
    assert (
        closing_fingerprint("CNC-01", s, e, 7, 3)
        == "6fed715cba4c9490144e32a304801dee5f604ec387ca3f5e888e3f376d85f99f"
    )
    assert (
        maintenance_plan_fingerprint("CNC-01", s, e, [(e, _ts(12)), (s, e)])
        == "c8e8ac785cffdd286628fa8b0a1fd64df5b29288c3ee741abbfbca54c9485575"
    )


# ---------------------------------------------------------------- 等价时区 → 同一指纹


def test_equivalent_timezones_same_event_fingerprint():
    fps = {event_fingerprint(s, e, "failure") for s, e in SAME_INSTANT}
    assert len(fps) == 1


def test_equivalent_timezones_same_ruling_fingerprint():
    fps = {ruling_fingerprint(s, e, "pending", "说明") for s, e in SAME_INSTANT}
    assert len(fps) == 1


def test_equivalent_timezones_same_allocation_fingerprint():
    base = allocation_fingerprint([1], [("B1", _ts(10), _ts(11))])
    for s, e in SAME_INSTANT:
        assert allocation_fingerprint([1], [("B1", s, e)]) == base


def test_equivalent_timezones_same_closing_fingerprint():
    base = closing_fingerprint("CNC-01", _ts(10), _ts(11), 2, 1)
    for s, e in SAME_INSTANT:
        assert closing_fingerprint("CNC-01", s, e, 2, 1) == base


def test_equivalent_timezones_same_plan_fingerprint():
    base = maintenance_plan_fingerprint(
        "CNC-01", _ts(0), _ts(23), [(_ts(10), _ts(11))]
    )
    alt = maintenance_plan_fingerprint(
        "CNC-01",
        datetime(2026, 9, 30, 16, 0, tzinfo=UTC),
        datetime(2026, 10, 1, 15, 0, tzinfo=UTC),
        [(datetime(2026, 10, 1, 2, 0, tzinfo=UTC), datetime(2026, 10, 1, 3, 0, tzinfo=UTC))],
    )
    assert alt == base


# ---------------------------------------------------------------- 允许换序的列表 → 同一指纹


def test_allocation_order_independent():
    batches = [("B1", _ts(10), _ts(11)), ("B2", _ts(11), _ts(12)), ("B3", _ts(12), _ts(13))]
    fp1 = allocation_fingerprint([3, 1, 2], batches)
    fp2 = allocation_fingerprint([2, 3, 1], list(reversed(batches)))
    fp3 = allocation_fingerprint([1, 2, 3], [batches[1], batches[2], batches[0]])
    assert fp1 == fp2 == fp3


def test_plan_window_order_independent():
    windows = [(_ts(11), _ts(12)), (_ts(10), _ts(11)), (_ts(13), _ts(14))]
    fp1 = maintenance_plan_fingerprint("CNC-01", _ts(0), _ts(23), windows)
    fp2 = maintenance_plan_fingerprint("CNC-01", _ts(0), _ts(23), list(reversed(windows)))
    assert fp1 == fp2


def test_rate_lines_canonical_order():
    class Line:
        def __init__(self, reason, start, end):
            self.reason, self.start, self.end = reason, start, end

    a = Line("failure", _ts(10), _ts(11))
    b = Line("maintenance", _ts(10), _ts(11))  # 同起点：按原因决胜
    c = Line("failure", _ts(9), _ts(10))
    d = Line("failure", _ts(9), _ts(9, 30))  # 同起点同原因：按终点决胜
    ordered = canonical_rate_lines([a, b, c, d])
    assert ordered == [d, c, a, b]
    # 换序提交得到同一规范化顺序
    assert canonical_rate_lines([d, b, a, c]) == ordered


# ---------------------------------------------------------------- 省略默认值 → 同一指纹


def test_ruling_note_omitted_equals_empty():
    assert ruling_fingerprint(_ts(10), _ts(11), "pending", None) == ruling_fingerprint(
        _ts(10), _ts(11), "pending", ""
    )


# ---------------------------------------------------------------- 业务含义差异 → 不同指纹（保留冲突）


def test_event_content_differences_change_fingerprint():
    base = event_fingerprint(_ts(10), _ts(11), "failure")
    assert event_fingerprint(_ts(10, 30), _ts(11), "failure") != base  # 起点不同
    assert event_fingerprint(_ts(10), _ts(11, 30), "failure") != base  # 终点不同
    assert event_fingerprint(_ts(10), _ts(11), "maintenance") != base  # 原因不同


def test_ruling_content_differences_change_fingerprint():
    base = ruling_fingerprint(_ts(10), _ts(11), "pending", "说明")
    assert ruling_fingerprint(_ts(10), _ts(12), "pending", "说明") != base
    assert ruling_fingerprint(_ts(10), _ts(11), "failure", "说明") != base
    assert ruling_fingerprint(_ts(10), _ts(11), "pending", "另一条说明") != base


def test_allocation_content_differences_change_fingerprint():
    batches = [("B1", _ts(10), _ts(11))]
    base = allocation_fingerprint([1, 2], batches)
    assert allocation_fingerprint([1, 3], batches) != base  # 账引用不同
    assert allocation_fingerprint([1, 2, 3], batches) != base  # 账数量不同
    assert allocation_fingerprint([1, 2], [("B2", _ts(10), _ts(11))]) != base  # 批次号不同
    assert (
        allocation_fingerprint([1, 2], [("B1", _ts(10, 30), _ts(11))]) != base
    )  # 窗口不同
    assert (
        allocation_fingerprint([1, 2], batches + [("B2", _ts(11), _ts(12))]) != base
    )  # 批次数量不同


def test_closing_input_differences_change_fingerprint():
    base = closing_fingerprint("CNC-01", _ts(0), _ts(23), 2, 1)
    assert closing_fingerprint("CNC-02", _ts(0), _ts(23), 2, 1) != base  # 设备不同
    assert closing_fingerprint("CNC-01", _ts(0), _ts(23, 30), 2, 1) != base  # 期间不同
    assert closing_fingerprint("CNC-01", _ts(0), _ts(23), 3, 1) != base  # 截止序号不同
    assert closing_fingerprint("CNC-01", _ts(0), _ts(23), 2, 2) != base  # 费率版本不同


def test_plan_content_differences_change_fingerprint():
    windows = [(_ts(10), _ts(11))]
    base = maintenance_plan_fingerprint("CNC-01", _ts(0), _ts(23), windows)
    assert maintenance_plan_fingerprint("CNC-02", _ts(0), _ts(23), windows) != base
    assert maintenance_plan_fingerprint("CNC-01", _ts(0), _ts(22), windows) != base
    assert (
        maintenance_plan_fingerprint("CNC-01", _ts(0), _ts(23), [(_ts(10), _ts(12))])
        != base
    )
    assert (
        maintenance_plan_fingerprint(
            "CNC-01", _ts(0), _ts(23), windows + [(_ts(11), _ts(12))]
        )
        != base
    )


# ---------------------------------------------------------------- 不放宽校验 / 不抹平差异


def test_sub_minute_difference_not_erased():
    """指纹层不做整分钟对齐：秒级差异必须产生不同指纹（校验在上游拒绝）。"""
    whole = event_fingerprint(_ts(10), _ts(11), "failure")
    with_seconds = event_fingerprint(
        datetime(2026, 10, 1, 10, 0, 30, tzinfo=TZ8), _ts(11), "failure"
    )
    assert with_seconds != whole


def test_utc_iso_keeps_offset_normalized():
    assert utc_iso(_ts(10, tz=TZ8)) == "2026-10-01T02:00:00+00:00"
    assert utc_iso(datetime(2026, 10, 1, 2, 0, tzinfo=UTC)) == "2026-10-01T02:00:00+00:00"


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
