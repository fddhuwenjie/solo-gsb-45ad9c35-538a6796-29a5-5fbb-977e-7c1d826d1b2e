"""核心换算引擎测试（数值正确性 / 往返 / 校验）。"""

import math

import pytest

from app.tempo import TempoMapError, build_tempo_map

PPQ = 480


def make(ppq=PPQ, meters=None, points=None):
    points = points if points is not None else [
        {"position": "1:1:0", "bpm": 120.0, "curve": "step"}
    ]
    return build_tempo_map(ppq, meters or [], points)


def test_quarter_note_coordinates_basic():
    tmap = make()  # 120 BPM 恒定
    # 4/4 小节：4 拍 = 4*480 = 1920 ticks
    assert tmap.bar_start_ticks(1) == 0
    assert tmap.bar_start_ticks(2) == 1920
    assert tmap.mbt_to_ticks("1:2:0") == 480
    assert tmap.mbt_to_ticks("2:1:0") == 1920
    assert str(tmap.ticks_to_mbt(1920)) == "2:1:0"
    assert str(tmap.ticks_to_mbt(480)) == "1:2:0"
    assert str(tmap.ticks_to_mbt(1919)) == "1:4:479"


def test_seconds_step_tempo():
    tmap = make()  # 120 BPM：一拍 0.5 秒，1 tick = 0.5/480 秒
    assert tmap.seconds_at_tick(0) == 0.0
    assert tmap.seconds_at_tick(480) == pytest.approx(0.5)
    assert tmap.seconds_at_tick(1920) == pytest.approx(2.0)
    # 末点后保持 120 BPM
    assert tmap.seconds_at_tick(1920 * 10 + 240) == pytest.approx(20.25)


def test_inverse_step_tempo():
    tmap = make()
    for s in [0.0, 0.5, 2.0, 7.777, 100.0]:
        assert tmap.tick_at_seconds(s) == pytest.approx(s * 120 * PPQ / 60)


def test_step_change_at_barline():
    tmap = make(points=[
        {"position": "1:1:0", "bpm": 120, "curve": "step"},
        # 第二小节首拍变为 60（一拍 1 秒）
        {"position": "2:1:0", "bpm": 60, "curve": "step"},
    ])
    # 第一小节 2.0 秒；第二小节每拍 1 秒
    assert tmap.seconds_at_tick(1920) == pytest.approx(2.0)
    assert tmap.seconds_at_tick(1920 + 480) == pytest.approx(3.0)
    # 边界时间 2.0 秒反解应落在 2:1:0（归属后段）
    assert tmap.tick_at_seconds(2.0) == pytest.approx(1920)
    assert tmap.seconds_at_tick(tmap.bar_start_ticks(5)) == pytest.approx(
        2.0 + 3 * 4 * 1.0
    )


def test_linear_ramp_integral():
    # 60 -> 120 在 480 ticks（一拍）内线性
    tmap = make(points=[
        {"position": "1:1:0", "bpm": 60, "curve": "linear"},
        {"position": "1:2:0", "bpm": 120, "curve": "step"},
        {"position": "2:1:0", "bpm": 120, "curve": "step"},
    ])
    # ΔT = 60*480/(60*480) * ln(120/60) = ln(2)
    assert tmap.seconds_at_tick(480) == pytest.approx(math.log(2))
    # 中间位置 B=90：ΔT = ln(90/60) = ln(1.5)
    assert tmap.seconds_at_tick(240) == pytest.approx(math.log(1.5))
    # 线性段之后恒定 120
    assert tmap.seconds_at_tick(960) == pytest.approx(math.log(2) + 0.5)


def test_linear_ramp_inverse_and_boundaries():
    tmap = make(points=[
        {"position": "1:1:0", "bpm": 60, "curve": "linear"},
        {"position": "1:2:0", "bpm": 120, "curve": "step"},
    ])
    for tick in [0, 1, 120, 240, 479, 480, 481, 2000]:
        t = tmap.seconds_at_tick(tick)
        back = tmap.tick_at_seconds(t)
        assert back == pytest.approx(float(tick), abs=1e-9), (tick, t, back)

    # 减速段同样成立
    tmap2 = make(points=[
        {"position": "1:1:0", "bpm": 240, "curve": "linear"},
        {"position": "1:3:0", "bpm": 40, "curve": "linear"},
        {"position": "2:2:0", "bpm": 96, "curve": "step"},
    ])
    for tick in range(0, tmap2.bar_start_ticks(4), 37):
        t = tmap2.seconds_at_tick(tick)
        assert tmap2.tick_at_seconds(t) == pytest.approx(float(tick), abs=1e-7)


def test_meter_change_3_4():
    tmap = make(meters=[{"measure": 3, "numerator": 3, "denominator": 4}])
    # 1、2 小节 4/4 = 1920；第 3 小节起 3/4 = 1440
    assert tmap.bar_start_ticks(3) == 3840
    assert tmap.bar_start_ticks(4) == 3840 + 1440
    assert str(tmap.ticks_to_mbt(3840)) == "3:1:0"
    assert str(tmap.ticks_to_mbt(3840 + 1439)) == "3:3:479"
    assert str(tmap.ticks_to_mbt(3840 + 1440)) == "4:1:0"


def test_compound_meter_6_8():
    # 6/8：拍按分母计 = 八分音符，每拍 4*480/8 = 240 tick，每小节 6 拍
    tmap = make(meters=[{"measure": 1, "numerator": 6, "denominator": 8}])
    assert tmap.bar_start_ticks(2) == 6 * 240
    # 第 4 拍 = 3 个八分音符 = 720 tick
    assert tmap.mbt_to_ticks("1:4:0") == 720
    assert str(tmap.ticks_to_mbt(720)) == "1:4:0"
    assert str(tmap.ticks_to_mbt(6 * 240 - 1)) == "1:6:239"
    assert str(tmap.ticks_to_mbt(6 * 240)) == "2:1:0"


def test_compound_meter_6_8_position_to_seconds():
    # PPQ=480、6/8、120 BPM：1:4:0 -> 720 tick -> 0.75 秒
    tmap = make(meters=[{"measure": 1, "numerator": 6, "denominator": 8}])
    total = tmap.mbt_to_ticks("1:4:0")
    assert total == 720
    assert tmap.seconds_at_tick(total) == pytest.approx(0.75)
    # 反向：0.75 秒 -> 720 tick -> 1:4:0
    assert tmap.tick_at_seconds(0.75) == pytest.approx(720.0)
    assert str(tmap.ticks_to_mbt(720)) == "1:4:0"


def test_compound_meter_beat_and_tick_bounds():
    tmap = make(meters=[{"measure": 1, "numerator": 6, "denominator": 8}])
    # 合法边界：第 6 拍、单拍内最大 tick
    assert tmap.mbt_to_ticks("1:6:239") == 6 * 240 - 1
    # 超过第 6 拍 -> 422
    expect_error(lambda: tmap.mbt_to_ticks("1:7:0"), needle_msg="拍必须在 1..6 内")
    # tick 超出单拍范围（240）-> 422
    expect_error(lambda: tmap.mbt_to_ticks("1:1:240"), needle_msg="[0, 240)")
    # 拍号表中的速度点位置同样按该规则校验（带索引）
    expect_error(
        lambda: make(
            meters=[{"measure": 1, "numerator": 6, "denominator": 8}],
            points=[
                {"position": "1:1:0", "bpm": 120, "curve": "step"},
                {"position": "2:7:0", "bpm": 100, "curve": "step"},
            ],
        ),
        needle_index=1,
        needle_msg="拍必须在 1..6 内",
    )


def test_default_implicit_4_4():
    tmap = make(meters=[{"measure": 5, "numerator": 2, "denominator": 4}])
    # 前 4 小节隐含 4/4
    assert tmap.bar_start_ticks(5) == 4 * 4 * PPQ
    assert tmap.bar_start_ticks(6) == 4 * 4 * PPQ + 2 * PPQ


def test_position_semantics_with_meters():
    tmap = make(meters=[{"measure": 2, "numerator": 6, "denominator": 8}])
    # 第 1 小节隐含 4/4 = 1920；第 2 小节起 6/8：每拍 240 tick，每小节 1440
    assert tmap.mbt_to_ticks("2:1:0") == 4 * PPQ
    assert tmap.mbt_to_ticks("2:3:0") == 4 * PPQ + 2 * 240
    assert tmap.mbt_to_ticks("3:1:0") == 4 * PPQ + 6 * 240
    # 2:4:0 是 6/8 的合法第 4 拍（不再溢出到下一小节）
    assert tmap.mbt_to_ticks("2:4:0") == 4 * PPQ + 3 * 240
    assert str(tmap.ticks_to_mbt(4 * PPQ + 3 * 240)) == "2:4:0"
    assert str(tmap.ticks_to_mbt(tmap.mbt_to_ticks("3:2:0"))) == "3:2:0"
    # 拍号区域边界归属：6/8 区域起点 tick 规范到 2:1:0
    assert str(tmap.ticks_to_mbt(4 * PPQ)) == "2:1:0"


def test_tempo_point_at_compound_meter_position():
    tmap = make(
        meters=[{"measure": 1, "numerator": 7, "denominator": 8}],
        points=[
            {"position": "1:1:0", "bpm": 100, "curve": "step"},
            {"position": "2:1:0", "bpm": 200, "curve": "linear"},
            {"position": "3:1:0", "bpm": 200, "curve": "step"},
        ],
    )
    bar = 7 * (4 * PPQ // 8)  # 7/8 = 7 个八分音符拍 = 7*240 = 1680 ticks
    assert tmap.bar_start_ticks(2) == bar
    assert tmap.ticks == [0, bar, 2 * bar]


# ----------------------------- 校验错误 ----------------------------- #

def expect_error(fn, needle_index=None, needle_msg=None):
    with pytest.raises(TempoMapError) as ei:
        fn()
    errs = ei.value.errors
    assert errs, "至少应有一个错误"
    for e in errs:
        assert "loc" in e and "msg" in e and "index" in e
    if needle_index is not None:
        assert any(e["index"] == needle_index for e in errs)
    if needle_msg is not None:
        assert any(needle_msg in e["msg"] for e in errs)
    return errs


def test_first_point_must_be_origin():
    expect_error(
        lambda: make(points=[{"position": "1:2:0", "bpm": 120, "curve": "step"}]),
        needle_index=0,
        needle_msg="1:1:0",
    )


def test_non_increasing_tempo_points():
    expect_error(
        lambda: make(points=[
            {"position": "1:1:0", "bpm": 120, "curve": "step"},
            {"position": "2:1:0", "bpm": 100, "curve": "step"},
            {"position": "2:1:0", "bpm": 90, "curve": "step"},
        ]),
        needle_index=2,
        needle_msg="严格递增",
    )


def test_non_positive_bpm_indexed():
    expect_error(
        lambda: make(points=[
            {"position": "1:1:0", "bpm": 120, "curve": "step"},
            {"position": "2:1:0", "bpm": 0, "curve": "step"},
        ]),
        needle_index=1,
    )
    expect_error(
        lambda: make(points=[
            {"position": "1:1:0", "bpm": -5, "curve": "linear"},
        ]),
        needle_index=0,
    )


def test_bad_meter_denominator():
    expect_error(
        lambda: make(meters=[{"measure": 2, "numerator": 1, "denominator": 3}]),
        needle_index=0,
        needle_msg="2 的幂",
    )


def test_imprecise_meter_for_ppq():
    # PPQ=384 时 1/32: 单拍 4*384/32 = 48 ticks（整数，OK）
    build_tempo_map(384, [{"measure": 1, "numerator": 1, "denominator": 32}],
                    [{"position": "1:1:0", "bpm": 120, "curve": "step"}])
    # PPQ=480 时 7/8：单拍 240 ticks（整数，OK）
    build_tempo_map(480, [{"measure": 1, "numerator": 7, "denominator": 8}],
                    [{"position": "1:1:0", "bpm": 120, "curve": "step"}])
    # PPQ=100 时 1/32 -> 单拍 400/32 = 12.5 tick，不可整除
    expect_error(
        lambda: build_tempo_map(
            100,
            [{"measure": 1, "numerator": 1, "denominator": 32}],
            [{"position": "1:1:0", "bpm": 120, "curve": "step"}],
        ),
        needle_msg="整数 tick",
    )
    # PPQ=100 时 2/32 -> 小节长度 25 tick 虽为整数，但单拍 12.5 tick 仍拒绝
    expect_error(
        lambda: build_tempo_map(
            100,
            [{"measure": 1, "numerator": 2, "denominator": 32}],
            [{"position": "1:1:0", "bpm": 120, "curve": "step"}],
        ),
        needle_msg="单拍长度不是整数 tick",
    )
    # PPQ=480 时 1/256 -> 单拍 1920/256 = 7.5 tick，不可整除
    expect_error(
        lambda: build_tempo_map(
            480,
            [{"measure": 1, "numerator": 1, "denominator": 256}],
            [{"position": "1:1:0", "bpm": 120, "curve": "step"}],
        ),
        needle_msg="整数 tick",
    )
    # PPQ=481（奇数）时 7/8 -> 单拍 240.5 tick，无法用整数 tick 表示
    expect_error(
        lambda: build_tempo_map(
            481,
            [{"measure": 1, "numerator": 7, "denominator": 8}],
            [{"position": "1:1:0", "bpm": 120, "curve": "step"}],
        ),
        needle_msg="整数 tick",
    )


def test_non_increasing_meters():
    expect_error(
        lambda: make(meters=[
            {"measure": 3, "numerator": 4, "denominator": 4},
            {"measure": 2, "numerator": 3, "denominator": 4},
        ]),
        needle_index=1,
        needle_msg="严格递增",
    )


def test_bad_position_format():
    expect_error(
        lambda: make(points=[
            {"position": "1::0", "bpm": 120, "curve": "step"},
        ]),
        needle_index=0,
    )
    expect_error(
        lambda: make(points=[
            {"position": "1:1:480", "bpm": 120, "curve": "step"},  # tick 越界
        ]),
        needle_index=0,
    )


# ----------------------------- 往返误差 ----------------------------- #

def test_roundtrip_within_one_tick_rich_map():
    import random

    random.seed(7)
    meters = [
        {"measure": 1, "numerator": 4, "denominator": 4},
        {"measure": 5, "numerator": 3, "denominator": 4},
        {"measure": 9, "numerator": 7, "denominator": 8},
        {"measure": 13, "numerator": 6, "denominator": 8},
    ]
    points = [
        {"position": "1:1:0", "bpm": 80, "curve": "step"},
        {"position": "2:1:0", "bpm": 100, "curve": "linear"},
        {"position": "3:2:0", "bpm": 150, "curve": "step"},
        {"position": "6:1:0", "bpm": 60, "curve": "linear"},
        {"position": "10:2:0", "bpm": 220, "curve": "step"},
        {"position": "14:1:0", "bpm": 120, "curve": "linear"},
        {"position": "16:1:0", "bpm": 90, "curve": "step"},
    ]
    tmap = make(meters=meters, points=points)

    end_tick = tmap.bar_start_ticks(40)
    for tick in range(0, end_tick, 73):
        s = tmap.seconds_at_tick(tick)
        back = tmap.tick_at_seconds(s)
        assert abs(back - tick) <= 1e-7, (tick, s, back)
        # 量化后与 mbt 互转一致
        q = int(math.floor(back + 0.5))
        assert abs(q - tick) <= 1
        assert tmap.mbt_to_ticks(str(tmap.ticks_to_mbt(q))) == q

    # 随机秒数反解 -> 再正算，残差应在一个 tick 内的时间量之内
    total_seconds = tmap.seconds_at_tick(end_tick)
    for _ in range(300):
        s = random.uniform(0.0, total_seconds)
        raw = tmap.tick_at_seconds(s)
        q = int(math.floor(raw + 0.5))
        s2 = tmap.seconds_at_tick(q)
        # 时间差不超过该处一个 tick 的时长
        mbt = tmap.ticks_to_mbt(q)
        # 用相邻 tick 时间差估计局部 tick 时长
        local_dt = abs(tmap.seconds_at_tick(q + 1) - tmap.seconds_at_tick(q))
        assert abs(s2 - s) <= local_dt + 1e-9, (s, raw, q)
