"""乐曲速度图换算核心引擎。

约定
----
* PPQ = 每个四分音符 (quarter note) 的 tick 数。
* 乐谱位置 ``小节:拍:tick``（``m:b:t``）中的“拍”按该小节生效拍号的
  **分母**计：分母为 d 时一拍是 1/d 音符，即 ``4*PPQ/d`` tick
  （4/4 一拍 = PPQ tick；6/8 一拍 = PPQ/2 tick）。
  ``b`` 取 ``1..拍号分子``，``t`` 取 ``[0, 4*PPQ/d)``；超出即 422。
* 内部统一使用“距乐谱起点的 tick 数”（PPQ 坐标，整数）作为横坐标。
* 拍号只在小节首拍生效；第一份拍号表之前隐含 4/4。
  单拍长度 = 4*PPQ/denominator，小节长度 = numerator * 单拍长度；
  若 4*PPQ/denominator 不是整数 tick，则该拍号无法精确表示，返回 422。
* 正向展开（mbt->ticks）、速度点定位、反向规范化（ticks->mbt）与
  边界归属共用同一坐标规则：tick 落在区域/小节/拍边界时归后一段。
* 速度点 i 的曲线决定区间 [tick_i, tick_{i+1}) 内的速度；
  边界 tick_{i+1} 归属于后一个点（右连续），正反向保持一致。
* 末个速度点之后保持其 BPM（恒定）。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass

# 线性段两端 BPM 相对差小于该阈值时，退化为恒定速度（等价的数学极限），
# 避免分母过小时的浮点误差。
_EPS_REL = 1e-12


class TempoMapError(ValueError):
    """携带一组带定位信息（含数组索引）的校验错误。"""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__(
            "; ".join(f"{'/'.join(map(str, e['loc']))}: {e['msg']}" for e in errors)
        )


def make_error(msg: str, *loc: object, index: int | None = None) -> dict:
    return {"loc": list(loc), "msg": msg, "index": index}


@dataclass(frozen=True)
class _MeterBound:
    """拍号区域：从第 measure 小节（首拍 tick = start_tick）起生效。"""

    measure: int
    start_tick: int
    bar_ticks: int
    beat_ticks: int
    numerator: int
    denominator: int


@dataclass(frozen=True)
class MBT:
    measure: int
    beat: int
    tick: int

    def __str__(self) -> str:
        return f"{self.measure}:{self.beat}:{self.tick}"


def parse_mbt(text: str, loc: tuple[object, ...]) -> tuple[int, int, int]:
    """解析 ``小节:拍:tick``，返回 (measure, beat, tick)，非法时抛 422 错误。

    仅做语法与下界校验；拍/tick 的上界取决于该小节生效的拍号，
    由 :meth:`TempoMap.mbt_to_ticks` 按同一坐标规则统一校验。
    """

    raw = str(text).strip()
    parts = raw.split(":")
    if len(parts) != 3:
        raise TempoMapError(
            [make_error("位置格式应为 '小节:拍:tick'，例如 1:1:0", *loc)]
        )
    try:
        measure, beat, tick = (int(p) for p in parts)
    except ValueError:
        raise TempoMapError(
            [make_error("位置的三个分量都必须是整数，例如 1:1:0", *loc)]
        ) from None
    if measure < 1:
        raise TempoMapError(
            [make_error("小节号必须 >= 1", *loc)]
        )
    if beat < 1:
        raise TempoMapError(
            [make_error("拍必须 >= 1", *loc)]
        )
    if tick < 0:
        raise TempoMapError(
            [make_error("tick 必须 >= 0", *loc)]
        )
    return measure, beat, tick


class TempoMap:
    def __init__(
        self,
        ppq: int,
        meter_bounds: list[_MeterBound],
        ticks: list[int],
        bpms: list[float],
        curves: list[str],
    ):
        self.ppq = ppq
        self._meters = meter_bounds
        self._meter_measures = [m.measure for m in meter_bounds]
        self._meter_starts = [m.start_tick for m in meter_bounds]

        self.ticks = ticks
        self.bpms = bpms
        self.curves = curves

        # 各速度点对应的累计时间（秒），prefix[i] 即 ticks[i] 处的时间。
        self.prefix: list[float] = [0.0]
        for i in range(len(ticks) - 1):
            self.prefix.append(
                self.prefix[-1] + self._segment_seconds(i, ticks[i + 1] - ticks[i])
            )

    # ------------------------------------------------------------------ #
    # 拍号 / 位置坐标
    # ------------------------------------------------------------------ #
    def _meter_region(self, measure: int) -> int:
        """返回 measure 小节所属的拍号区域下标。"""
        return max(bisect_right(self._meter_measures, measure) - 1, 0)

    def bar_start_ticks(self, measure: int) -> int:
        """第 measure 小节首拍距乐谱起点的 tick 数。"""
        k = self._meter_region(measure)
        bound = self._meters[k]
        return bound.start_tick + (measure - bound.measure) * bound.bar_ticks

    def mbt_to_ticks(self, text: str, loc: tuple[object, ...] = ("position",)) -> int:
        """把 ``小节:拍:tick`` 展开为绝对 tick（拍按该小节拍号分母计）。"""
        measure, beat, tick = parse_mbt(text, loc)
        bound = self._meters[self._meter_region(measure)]
        if beat > bound.numerator:
            raise TempoMapError(
                [
                    make_error(
                        f"第 {measure} 小节生效拍号为 "
                        f"{bound.numerator}/{bound.denominator}，"
                        f"拍必须在 1..{bound.numerator} 内，收到 {beat}",
                        *loc,
                    )
                ]
            )
        if tick >= bound.beat_ticks:
            raise TempoMapError(
                [
                    make_error(
                        f"tick 必须在 [0, {bound.beat_ticks}) 内"
                        f"（{bound.numerator}/{bound.denominator} 拍号、"
                        f"PPQ={self.ppq} 下每拍 {bound.beat_ticks} tick），"
                        f"收到 {tick}",
                        *loc,
                    )
                ]
            )
        return (
            self.bar_start_ticks(measure)
            + (beat - 1) * bound.beat_ticks
            + tick
        )

    def ticks_to_mbt(self, ticks: int) -> MBT:
        """把绝对 tick 坐标规范成 小节:拍:tick（拍按该小节拍号分母计）。

        边界归属：tick 恰好落在拍号区域/小节/拍边界时归后一段，
        与 :meth:`mbt_to_ticks` 的展开规则互为逆运算。
        """
        k = max(bisect_right(self._meter_starts, ticks) - 1, 0)
        bound = self._meters[k]
        offset = ticks - bound.start_tick
        measure = bound.measure + offset // bound.bar_ticks
        within = offset % bound.bar_ticks
        beat = within // bound.beat_ticks + 1
        tick = within % bound.beat_ticks
        return MBT(measure, beat, tick)

    # ------------------------------------------------------------------ #
    # 时间换算
    # ------------------------------------------------------------------ #
    def _segment_seconds(self, i: int, dticks: int | float) -> float:
        """速度段 i 内走过 dticks 个 tick 所用的秒数。"""
        b0 = self.bpms[i]
        is_linear = (
            self.curves[i] == "linear" and i < len(self.bpms) - 1 and dticks != 0
        )
        if not is_linear:
            # 阶梯段、线性末点之后的保持段：恒定 BPM。
            return 60.0 * dticks / (b0 * self.ppq)

        span = self.ticks[i + 1] - self.ticks[i]
        b1 = self.bpms[i + 1]
        db = b1 - b0
        if span <= 0 or abs(db) <= _EPS_REL * max(1.0, abs(b0), abs(b1)):
            return 60.0 * dticks / (b0 * self.ppq)

        # B(t) = b0 + db * (t - t0) / span
        # ΔT = ∫ 60 / (B(t)·PPQ) dt = 60·span/(db·PPQ) · ln(B(t)/b0)
        b_at_t = b0 + db * (dticks / span)
        return 60.0 * span / (db * self.ppq) * math.log(b_at_t / b0)

    def seconds_at_tick(self, tick: int | float) -> float:
        """位置（绝对 tick）-> 秒。"""
        i = max(bisect_right(self.ticks, tick) - 1, 0)
        return self.prefix[i] + self._segment_seconds(i, tick - self.ticks[i])

    def tick_seconds_at(self, tick: int | float) -> float:
        """位置（绝对 tick）处**一个 tick** 的时长（秒）。

        即该 tick 与下一 tick 的时间差，直接复用 :meth:`seconds_at_tick`
        （阶梯段恒定、线性段解析积分），边界归属与正反向换算保持一致。
        录音锚点录入时用它记录“该位置一 tick 的时长”，作为锁定偏移阈值。
        """
        return self.seconds_at_tick(tick + 1.0) - self.seconds_at_tick(tick)

    def tick_at_seconds(self, seconds: float) -> float:
        """秒 -> 位置（绝对 tick，浮点，未量化）。在同一速度段内解析求逆。"""
        if not math.isfinite(seconds) or seconds < 0:
            raise TempoMapError(
                [make_error("seconds 必须是有限的非负数", "body", "seconds")]
            )

        n = len(self.ticks)
        # 边界时间归后段：s == prefix[i+1] 时会落到段 i+1（dt=0），与正向一致。
        i = bisect_right(self.prefix, seconds) - 1
        if i < 0:
            i = 0

        elapsed = seconds - self.prefix[i]
        b0 = self.bpms[i]

        # 末点之后保持 BPM；阶梯段恒定 BPM。
        if i >= n - 1 or self.curves[i] != "linear":
            return self.ticks[i] + elapsed * b0 * self.ppq / 60.0

        span = self.ticks[i + 1] - self.ticks[i]
        b1 = self.bpms[i + 1]
        db = b1 - b0
        if span <= 0 or abs(db) <= _EPS_REL * max(1.0, abs(b0), abs(b1)):
            return self.ticks[i] + elapsed * b0 * self.ppq / 60.0

        # elapsed = A·ln(B/b0)，A = 60·span/(db·PPQ)
        # B = b0·exp(elapsed/A)；tick 关于 B 线性，按同段插值求逆。
        a = 60.0 * span / (db * self.ppq)
        b_at_t = b0 * math.exp(elapsed / a)
        return self.ticks[i] + span * (b_at_t - b0) / db


# ---------------------------------------------------------------------- #
# 构建（含全部带索引的语义校验）
# ---------------------------------------------------------------------- #
def _build_meter_bounds(
    ppq: int, raw_meters: list[dict]
) -> tuple[list[_MeterBound], list[dict]]:
    errors: list[dict] = []
    cleaned: list[tuple[int, int, int, int]] = []

    for i, m in enumerate(raw_meters):
        base = ("body", "time_signatures", i)
        measure = m["measure"]
        numerator = m["numerator"]
        denominator = m["denominator"]

        if not isinstance(measure, int) or measure < 1:
            errors.append(
                make_error("拍号生效小节必须是 >= 1 的整数", *base, "measure", index=i)
            )
        if not isinstance(numerator, int) or numerator < 1:
            errors.append(
                make_error("拍号分子必须是 >= 1 的整数", *base, "numerator", index=i)
            )
        if not isinstance(denominator, int) or denominator < 1:
            errors.append(
                make_error("拍号分母必须是 >= 1 的整数", *base, "denominator", index=i)
            )
        elif denominator & (denominator - 1):
            errors.append(
                make_error(
                    f"拍号分母必须是 2 的幂（2/4/8/16/...），收到 {denominator}",
                    *base,
                    "denominator",
                    index=i,
                )
            )

        if errors and any(e["index"] == i for e in errors):
            continue

        scaled = 4 * ppq
        if scaled % denominator != 0:
            errors.append(
                make_error(
                    f"拍号 {numerator}/{denominator} 在 PPQ={ppq} 下单拍长度不是整数 tick "
                    f"(4*{ppq}/{denominator})，无法精确表示",
                    *base,
                    index=i,
                )
            )
            continue
        cleaned.append((measure, numerator, denominator, scaled // denominator))

    # 小节号严格递增（拍号只在小节首拍生效，不允许乱序或同小节重复）。
    for i in range(1, len(cleaned)):
        if cleaned[i][0] <= cleaned[i - 1][0]:
            errors.append(
                make_error(
                    f"拍号表小节号必须严格递增：第 {i} 项小节 "
                    f"{cleaned[i][0]} 不晚于前一项 {cleaned[i - 1][0]}",
                    "body",
                    "time_signatures",
                    i,
                    "measure",
                    index=i,
                )
            )

    if errors:
        return [], errors

    if not cleaned or cleaned[0][0] != 1:
        cleaned.insert(0, (1, 4, 4, ppq))

    bounds: list[_MeterBound] = []
    cur_measure, cur_tick = 1, 0
    for k, (measure, numerator, denominator, beat_ticks) in enumerate(cleaned):
        bar_ticks = numerator * beat_ticks
        if k > 0:
            prev_bar = cleaned[k - 1][1] * cleaned[k - 1][3]
            cur_tick += (measure - cur_measure) * prev_bar
            cur_measure = measure
        bounds.append(
            _MeterBound(
                measure, cur_tick, bar_ticks, beat_ticks, numerator, denominator
            )
        )
    return bounds, []


def build_tempo_map(
    ppq: int,
    raw_meters: list[dict],
    raw_points: list[dict],
) -> TempoMap:
    errors: list[dict] = []

    if not isinstance(ppq, int) or isinstance(ppq, bool) or ppq < 1:
        raise TempoMapError(
            [make_error("PPQ 必须是 >= 1 的整数", "body", "ppq")]
        )

    meter_bounds, errors = _build_meter_bounds(ppq, raw_meters)
    # 即便拍号有错，也用 4/4 草稿继续解析速度点位置，以便一次返回所有索引错误。
    draft_bounds = meter_bounds or [
        _MeterBound(1, 0, 4 * ppq, ppq, 4, 4)
    ]
    draft = TempoMap(ppq, draft_bounds, [0], [1.0], ["step"])

    # 用定长槽位保存已解析结果，避免某些点因格式非法被跳过而造成下标错位。
    n = len(raw_points)
    ticks: list[int | None] = [None] * n
    bpms: list[float | None] = [None] * n
    curves: list[str | None] = [None] * n

    for i, p in enumerate(raw_points):
        base = ("body", "tempo_points", i)
        bpm = p["bpm"]
        curve = p["curve"]

        if not isinstance(bpm, (int, float)) or isinstance(bpm, bool):
            errors.append(
                make_error("BPM 必须是数字", *base, "bpm", index=i)
            )
        elif not math.isfinite(float(bpm)) or bpm <= 0:
            errors.append(
                make_error(f"BPM 必须是正数，收到 {bpm}", *base, "bpm", index=i)
            )
        else:
            bpms[i] = float(bpm)

        if curve not in ("step", "linear"):
            errors.append(
                make_error(
                    f"curve 只能是 'step' 或 'linear'，收到 {curve!r}",
                    *base,
                    "curve",
                    index=i,
                )
            )
        else:
            curves[i] = curve

        try:
            ticks[i] = draft.mbt_to_ticks(p["position"], loc=(*base, "position"))
        except TempoMapError as exc:
            for e in exc.errors:
                # loc 已带完整前缀，补上索引便于检索。
                e["index"] = i
                errors.append(e)

    if n == 0:
        errors.append(
            make_error("至少需要一个速度点", "body", "tempo_points")
        )
    else:
        if ticks[0] is not None and ticks[0] != 0:
            errors.append(
                make_error(
                    f"首个速度点必须位于 1:1:0（tick 0），收到 {raw_points[0]['position']!r}",
                    "body",
                    "tempo_points",
                    0,
                    "position",
                    index=0,
                )
            )
        for i in range(1, n):
            if ticks[i] is None or ticks[i - 1] is None:
                continue
            if ticks[i] <= ticks[i - 1]:
                errors.append(
                    make_error(
                        f"速度点位置必须严格递增：第 {i} 点 {raw_points[i]['position']} "
                        f"不晚于第 {i - 1} 点 {raw_points[i - 1]['position']}",
                        "body",
                        "tempo_points",
                        i,
                        "position",
                        index=i,
                    )
                )

    if errors:
        raise TempoMapError(errors)

    return TempoMap(
        ppq,
        meter_bounds,
        [t for t in ticks if t is not None],
        [b for b in bpms if b is not None],
        [c for c in curves if c is not None],
    )
