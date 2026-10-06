"""FastAPI 入口：POST /convert 速度图换算；谱例修订；排练提示领取。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import math
from typing import Literal, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .db import (
    ElapsedRegression,
    OriginMismatch,
    RehearsalNotFound,
    RevisionConflict,
    ScoreNotFound,
    Store,
)
from .tempo import TempoMap, TempoMapError, build_tempo_map

app = FastAPI(
    title="乐曲速度图换算 API",
    description=(
        "在乐谱位置（小节:拍:tick，拍按当前拍号分母对应的音符计）与秒数之间换算，"
        "支持阶梯（step）与线性（linear）BPM 速度点、拍号表及 PPQ；"
        "并提供带录音锚点的速度图修订（创建/预览/发布，乐观锁修订号），"
        "以及排练提示：为已保存的谱例创建排练，按谱例当前已提交的速度图给"
        "未发提示定时，客户端凭从排练开始累计的秒数领取到点提示"
        "（进度只前进，发放记录含所用修订号）；排练中途可定位（跳转）到"
        "另一乐谱位置：提交当前已领取累计秒数与目标位置，目标之前的未发"
        "提示记为跳过，此后提示按目标到提示的速度图时长相对该累计秒数定时。"
    ),
    version="1.3.0",
)

# SQLite 持久化（默认 ./data/tempo.db，可用 TEMPO_DB_PATH 覆盖；
# Docker Compose 中指向挂载卷 /data/tempo.db，重启后仍可读取）。
store = Store()


def _error_response(errors: list[dict], status_code: int = 422) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": errors})


@app.exception_handler(TempoMapError)
async def tempo_map_error_handler(_request: Request, exc: TempoMapError) -> JSONResponse:
    # 语义校验错误（含出错数组元素索引 index）。
    return _error_response(exc.errors)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Pydantic/请求体结构错误：保留 FastAPI 的 loc（天然含数组下标），
    # 同时展开为与语义错误一致的 {loc, msg, index} 结构。
    detail = []
    for err in exc.errors():
        loc = list(err.get("loc", []))
        index = next((x for x in reversed(loc) if isinstance(x, int)), None)
        detail.append(
            {
                "loc": [str(x) for x in loc],
                "msg": err.get("msg", "请求参数不合法"),
                "index": index,
            }
        )
    return _error_response(detail)


class TimeSignature(BaseModel):
    """只在小节首拍生效的拍号。第一份拍号前隐含 4/4。"""

    # 仅做类型约束；取值合法性（含数组下标）由引擎统一收集后返回 422。
    measure: int = Field(..., description="生效小节号（该小节首拍起）")
    numerator: int = Field(..., description="拍号分子，如 3/4 的 3；同时是每小节的拍数")
    denominator: int = Field(
        ...,
        description="拍号分母（2 的幂），如 3/4 的 4；一拍 = 4*PPQ/分母 tick，须为整数",
    )


class TempoPoint(BaseModel):
    """速度点。curve 决定该点到下一点区间内的速度走向。"""

    model_config = ConfigDict(json_schema_extra={})

    position: str = Field(
        ...,
        description=(
            "乐谱位置 '小节:拍:tick'，拍按当前拍号分母计"
            "（6/8 中一拍为八分音符），如 1:1:0、3:2:120"
        ),
    )
    bpm: float = Field(..., description="该点的 BPM，必须为正数")
    curve: Optional[Literal["step", "linear"]] = Field(
        "step",
        description=(
            "从该点到下一点：step=保持该 BPM（阶梯），linear=线性渐变到下一速度点。"
            "末点的曲线仅用于其后的保持段，实际按恒定 BPM 处理。"
        ),
    )


class ConvertRequest(BaseModel):
    ppq: int = Field(..., description="每个四分音符的 tick 数，如 480")
    time_signatures: list[TimeSignature] = Field(
        default_factory=list,
        description="拍号表，按小节首拍生效；空表或首项不在第 1 小节时前置隐含 4/4",
    )
    tempo_points: list[TempoPoint] = Field(
        ..., description="速度点表；首点必须在 1:1:0，位置严格递增"
    )
    position: Optional[str] = Field(
        None,
        description="待换算乐谱位置 '小节:拍:tick'（拍按当前拍号分母计）；与 seconds 二选一",
    )
    seconds: Optional[float] = Field(
        None, description="待换算时间（秒）；与 seconds 二选一（非负）"
    )


class ConvertResponse(BaseModel):
    ppq: int
    input_position: Optional[str]
    input_seconds: Optional[float]
    position: str = Field(..., description="规范乐谱位置 小节:拍:tick")
    measure: int
    beat: int
    tick: int
    total_ticks: int = Field(..., description="距乐谱起点的 tick 总数（PPQ 坐标）")
    seconds: float = Field(..., description="距乐谱起点的时间（秒）")
    roundtrip: "RoundTrip"


class RoundTrip(BaseModel):
    source: Literal["position", "seconds"]
    residual_ticks: float = Field(
        ...,
        description="位置 -> 秒 -> 位置（或反向）往返后，未量化位置与原位置的 tick 差绝对值",
    )
    residual_seconds: float = Field(..., description="以秒计的往返残差")
    position_roundtrip: str = Field(..., description="往返后再规范得到的乐谱位置")
    raw_total_ticks: float = Field(
        ..., description="秒 -> tick 反解出的未量化 tick（位置输入时等于 total_ticks）"
    )
    within_one_tick: bool = Field(..., description="往返误差是否不超过 1 tick")


ConvertResponse.model_rebuild()


# ---------------------------------------------------------------------- #
# 修订（录音锚点）相关模型
# ---------------------------------------------------------------------- #
class AnchorInput(BaseModel):
    """录音锚点：乐谱位置 + 锁定标志。

    秒数与“该位置一个 tick 的时长”由服务端按创建时的原图计算并冻结，
    不接受客户端传入。
    """

    position: str = Field(
        ...,
        description=(
            "锚点乐谱位置 '小节:拍:tick'（拍按当前拍号分母计）；"
            "创建时按原速度图计算并保存其秒数"
        ),
    )
    locked: bool = Field(
        ...,
        description="是否锁定录音点：发布新速度图时该点秒数保持创建时的值"
    )


class ScoreCreateRequest(BaseModel):
    ppq: int = Field(..., description="每个四分音符的 tick 数，如 480")
    time_signatures: list[TimeSignature] = Field(
        default_factory=list,
        description="拍号表，按小节首拍生效；空表或首项不在第 1 小节时前置隐含 4/4",
    )
    tempo_points: list[TempoPoint] = Field(
        ..., description="原速度点表；首点必须在 1:1:0，位置严格递增"
    )
    anchors: list[AnchorInput] = Field(
        ..., description="录音锚点表；至少有一个 locked=true 的锚点"
    )


class TempoReplaceRequest(BaseModel):
    tempo_points: list[TempoPoint] = Field(
        ...,
        description=(
            "整份替换速度点表（沿用创建时的 PPQ 与拍号校验）；"
            "首点必须在 1:1:0，位置严格递增"
        ),
    )


class AnchorInfo(BaseModel):
    index: int
    position: str
    locked: bool
    total_ticks: int = Field(..., description="锚点位置距起点的 tick 总数（PPQ 坐标）")
    original_seconds: float = Field(
        ..., description="创建时按原速度图计算并冻结的秒数；锁定锚点发布后保持不变"
    )
    current_seconds: float = Field(
        ..., description="当前修订速度图下该锚点的秒数（未锁定锚点随发布更新）"
    )
    tick_seconds: float = Field(
        ..., description="创建时该位置一个 tick 的时长（秒），即锁定偏移阈值"
    )


class ScoreOut(BaseModel):
    id: str
    revision: int = Field(..., description="当前修订号；创建时为 0，每次发布 +1")
    ppq: int
    time_signatures: list[TimeSignature]
    tempo_points: list[TempoPoint]
    anchors: list[AnchorInfo]


class AnchorPreview(BaseModel):
    index: int
    position: str
    locked: bool
    original_seconds: float = Field(..., description="创建时冻结的秒数（不修改已存数据）")
    new_seconds: float = Field(..., description="按候选替换速度图重新计算的秒数")
    offset_seconds: float = Field(
        ..., description="新秒数相对创建时冻结秒数的偏移（new - original，带符号）"
    )
    lock_conflict: bool = Field(
        ...,
        description=(
            "是否锁定冲突：锁定锚点的偏移绝对值是否严格超过"
            "该位置录入时一个 tick 的时长"
        ),
    )
    tick_seconds: float = Field(..., description="该锚点录入时一个 tick 的时长（秒）")


class PreviewResponse(BaseModel):
    id: str
    expected_revision: int
    current_revision: int
    tempo_points: list[TempoPoint] = Field(..., description="本次预览的候选替换速度点表")
    anchors: list[AnchorPreview]
    has_lock_conflict: bool = Field(
        ..., description="是否存在任一锁定锚点冲突（true 时发布会返回 422）"
    )


@app.get("/")
async def root() -> dict:
    return {
        "service": "tempo-map-converter",
        "docs": "/docs",
        "convert": "/convert",
        "scores": "/scores",
        "rehearsals": "/rehearsals",
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/convert", response_model=ConvertResponse)
async def convert(req: ConvertRequest) -> ConvertResponse:
    if req.seconds is not None and not (
        isinstance(req.seconds, (int, float))
        and not isinstance(req.seconds, bool)
        and math.isfinite(req.seconds)
        and req.seconds >= 0
    ):
        raise TempoMapError(
            [
                {
                    "loc": ["body", "seconds"],
                    "msg": "seconds 必须是有限的非负数",
                    "index": None,
                }
            ]
        )

    tmap = build_tempo_map(
        ppq=req.ppq,
        raw_meters=[m.model_dump() for m in req.time_signatures],
        raw_points=[
            {"position": p.position, "bpm": p.bpm, "curve": p.curve}
            for p in req.tempo_points
        ],
    )

    if (req.position is None) == (req.seconds is None):
        raise TempoMapError(
            [
                {
                    "loc": ["body"],
                    "msg": "position 与 seconds 必须且只能提供一个",
                    "index": None,
                }
            ]
        )

    if req.position is not None:
        total_ticks = tmap.mbt_to_ticks(req.position)
        seconds = tmap.seconds_at_tick(total_ticks)
        # 往返：位置 -> 秒 -> 位置
        raw_ticks = tmap.tick_at_seconds(seconds)
        back_seconds = tmap.seconds_at_tick(raw_ticks)
        input_position = req.position
        input_seconds = None
        source = "position"
    else:
        seconds = float(req.seconds)  # type: ignore[arg-type]
        raw_ticks = tmap.tick_at_seconds(seconds)
        total_ticks = int(math.floor(raw_ticks + 0.5))
        # 往返：秒 -> 位置（量化到 tick）-> 秒
        back_seconds = tmap.seconds_at_tick(total_ticks)
        input_position = None
        input_seconds = float(req.seconds)  # type: ignore[arg-type]
        source = "seconds"

    mbt = tmap.ticks_to_mbt(total_ticks)
    back_mbt = tmap.ticks_to_mbt(int(math.floor(raw_ticks + 0.5)))

    # 残差始终以“反解出的未量化 tick”与量化 tick 之差衡量（<= 1 tick 即合格）。
    residual_ticks = abs(raw_ticks - total_ticks)
    residual_seconds = abs(back_seconds - seconds)

    roundtrip = RoundTrip(
        source=source,
        residual_ticks=residual_ticks,
        residual_seconds=residual_seconds,
        position_roundtrip=str(back_mbt),
        raw_total_ticks=raw_ticks,
        within_one_tick=residual_ticks <= 1.0 + 1e-9,
    )

    return ConvertResponse(
        ppq=req.ppq,
        input_position=input_position,
        input_seconds=input_seconds,
        position=str(mbt),
        measure=mbt.measure,
        beat=mbt.beat,
        tick=mbt.tick,
        total_ticks=total_ticks,
        seconds=seconds,
        roundtrip=roundtrip,
    )


# ---------------------------------------------------------------------- #
# 带录音锚点的修订：创建 / 预览 / 发布 / 查询
# ---------------------------------------------------------------------- #
def _build_map(ppq: int, meters: list[dict], points: list[dict]) -> TempoMap:
    """沿用与 /convert 完全相同的拍号、速度点校验与线性段计时构建速度图。"""
    return build_tempo_map(ppq=ppq, raw_meters=meters, raw_points=points)


def _evaluate_replacement(
    candidate_map: TempoMap, anchors: list[dict], *, raise_on_conflict: bool
) -> list[dict]:
    """按候选速度图重新计算每个锚点（纯计算，不触碰已存数据）。

    锁定冲突：锁定锚点新秒数相对创建时冻结秒数的偏移绝对值，
    **严格超过**“该位置录入时一个 tick 的时长”即为冲突（发布时 422）。
    阈值取创建时冻结的 tick_seconds，而非候选图重算值——
    “该位置录入时”指锚点录入（谱例创建）那一刻。

    ``raise_on_conflict`` 为 True（发布）时聚合冲突错误并抛 422；
    为 False（预览）时不抛错，只在每个锚点上标记 lock_conflict。
    """
    evaluated: list[dict] = []
    position_errors: list[dict] = []
    conflict_errors: list[dict] = []
    for i, a in enumerate(anchors):
        base = ("body", "anchors", i)
        try:
            total_ticks = candidate_map.mbt_to_ticks(
                a["position"], loc=(*base, "position")
            )
        except TempoMapError as exc:
            for e in exc.errors:
                e["index"] = i
                position_errors.append(e)
            continue
        new_seconds = candidate_map.seconds_at_tick(total_ticks)
        original = a["original_seconds"]
        offset = new_seconds - original
        tick_seconds = a["tick_seconds"]
        # “超过”按严格大于处理；浮点比较给一个相对容差（约 1e-9）。
        tolerance = 1e-9 * max(1.0, abs(tick_seconds))
        lock_conflict = bool(
            a["locked"] and abs(offset) > tick_seconds + tolerance
        )
        if lock_conflict:
            conflict_errors.append(
                {
                    "loc": ["body", "anchors", i],
                    "msg": (
                        f"锁定锚点 {i}（{a['position']}）相对创建时保存的 "
                        f"{original:.6f}s 偏移 {offset:+.6f}s，"
                        f"超过该位置录入时一 tick 的时长 {tick_seconds:.9f}s"
                    ),
                    "index": i,
                }
            )
        evaluated.append(
            {
                "index": i,
                "position": a["position"],
                "locked": bool(a["locked"]),
                "total_ticks": total_ticks,
                "original_seconds": original,
                "current_seconds": new_seconds,
                "new_seconds": new_seconds,
                "offset_seconds": offset,
                "tick_seconds": tick_seconds,
                "lock_conflict": lock_conflict,
            }
        )
    # 位置非法始终 422（正常不会发生：PPQ/拍号沿用创建时的值，位置已校验）；
    # 锁定冲突仅在发布时（raise_on_conflict）返回 422，预览只做标记。
    errors = position_errors + (conflict_errors if raise_on_conflict else [])
    if errors:
        raise TempoMapError(errors)
    return evaluated


def _not_found(score_id: str) -> JSONResponse:
    return _error_response(
        [{"loc": ["path", "score_id"], "msg": f"谱例 {score_id} 不存在", "index": None}],
        status_code=404,
    )


def _conflict(exc: RevisionConflict) -> JSONResponse:
    # 预览后若已有其他发布：旧预期修订号仍须 409，不能覆盖新版本。
    detail = {
        "loc": ["path", "expected_revision"],
        "msg": f"修订号冲突：预期 {exc.expected}，当前 {exc.current}",
        "index": None,
        "expected_revision": exc.expected,
        "current_revision": exc.current,
    }
    return _error_response([detail], status_code=409)


@app.post("/scores", response_model=ScoreOut, status_code=201)
async def create_score(req: ScoreCreateRequest) -> ScoreOut:
    meters = [m.model_dump() for m in req.time_signatures]
    points = [
        {"position": p.position, "bpm": p.bpm, "curve": p.curve}
        for p in req.tempo_points
    ]
    if not req.anchors:
        raise TempoMapError(
            [
                {
                    "loc": ["body", "anchors"],
                    "msg": "至少需要一个录音锚点",
                    "index": None,
                }
            ]
        )
    if not any(a.locked for a in req.anchors):
        raise TempoMapError(
            [
                {
                    "loc": ["body", "anchors"],
                    "msg": "至少需要一个 locked=true 的锁定锚点",
                    "index": None,
                }
            ]
        )

    # 原速度图：沿用 /convert 的拍号/速度点校验。
    tmap = _build_map(req.ppq, meters, points)

    # 按原图计算并冻结各锚点秒数与该位置一 tick 的时长。
    anchor_rows: list[dict] = []
    anchor_errors: list[dict] = []
    for i, a in enumerate(req.anchors):
        base = ("body", "anchors", i)
        try:
            total_ticks = tmap.mbt_to_ticks(a.position, loc=(*base, "position"))
        except TempoMapError as exc:
            for e in exc.errors:
                e["index"] = i
                anchor_errors.append(e)
            continue
        seconds = tmap.seconds_at_tick(total_ticks)
        anchor_rows.append(
            {
                "position": a.position,
                "locked": a.locked,
                "total_ticks": total_ticks,
                "original_seconds": seconds,
                "tick_seconds": tmap.tick_seconds_at(total_ticks),
            }
        )
    if anchor_errors:
        raise TempoMapError(anchor_errors)

    score = store.create_score(req.ppq, meters, points, anchor_rows)
    return ScoreOut.model_validate(score)


@app.post(
    "/scores/{score_id}/revisions/{expected_revision}/preview",
    response_model=PreviewResponse,
)
async def preview_revision(
    score_id: str, expected_revision: int, req: TempoReplaceRequest
) -> PreviewResponse:
    try:
        score = store.get_score(score_id)
    except ScoreNotFound:
        return _not_found(score_id)  # type: ignore[return-value]

    if score["revision"] != expected_revision:
        return _conflict(  # type: ignore[return-value]
            RevisionConflict(expected_revision, score["revision"])
        )

    points = [
        {"position": p.position, "bpm": p.bpm, "curve": p.curve}
        for p in req.tempo_points
    ]
    # 候选表先过与 /convert 相同的校验；非法 -> 422。
    candidate_map = _build_map(score["ppq"], score["time_signatures"], points)
    # 预览只计算，不修改已存数据；存在锁定冲突也照常返回（422 留给发布）。
    evaluated = _evaluate_replacement(
        candidate_map, score["anchors"], raise_on_conflict=False
    )

    return PreviewResponse(
        id=score_id,
        expected_revision=expected_revision,
        current_revision=score["revision"],
        tempo_points=req.tempo_points,
        anchors=[
            AnchorPreview(
                index=a["index"],
                position=a["position"],
                locked=a["locked"],
                original_seconds=a["original_seconds"],
                new_seconds=a["new_seconds"],
                offset_seconds=a["offset_seconds"],
                lock_conflict=a["lock_conflict"],
                tick_seconds=a["tick_seconds"],
            )
            for a in evaluated
        ],
        has_lock_conflict=any(a["lock_conflict"] for a in evaluated),
    )


@app.post(
    "/scores/{score_id}/revisions/{expected_revision}/publish",
    response_model=ScoreOut,
)
async def publish_revision(
    score_id: str, expected_revision: int, req: TempoReplaceRequest
):
    try:
        score = store.get_score(score_id)
    except ScoreNotFound:
        return _not_found(score_id)

    # 乐观锁先于一切计算：修订号不符直接 409。
    if score["revision"] != expected_revision:
        return _conflict(RevisionConflict(expected_revision, score["revision"]))

    points = [
        {"position": p.position, "bpm": p.bpm, "curve": p.curve}
        for p in req.tempo_points
    ]
    candidate_map = _build_map(score["ppq"], score["time_signatures"], points)
    # 任一锁定锚点偏移超过录入时一 tick -> 422（带锚点索引），数据不变。
    evaluated = _evaluate_replacement(
        candidate_map, score["anchors"], raise_on_conflict=True
    )

    try:
        updated = store.publish_revision(
            score_id, expected_revision, points, evaluated
        )
    except ScoreNotFound:
        return _not_found(score_id)
    except RevisionConflict as exc:
        # 锁内重读发现已有其他发布：旧预期修订号仍 409，不能覆盖新版本。
        return _conflict(exc)
    return ScoreOut.model_validate(updated)


@app.get("/scores/{score_id}", response_model=ScoreOut)
async def get_score(score_id: str):
    """查询谱例：返回同一版本的速度图（修订号）与锚点。"""
    try:
        score = store.get_score(score_id)
    except ScoreNotFound:
        return _not_found(score_id)
    return ScoreOut.model_validate(score)


# ---------------------------------------------------------------------- #
# 排练提示：创建排练 / 领取到点提示 / 查询
# ---------------------------------------------------------------------- #
class CueInput(BaseModel):
    """排练提示：乐谱位置 + 文字。

    位置在创建排练时按谱例的 PPQ/拍号校验并展开为 tick 坐标
    （速度图修订只改速度点，不影响该坐标）。
    """

    position: str = Field(
        ...,
        description=(
            "提示的乐谱位置 '小节:拍:tick'（拍按当前拍号分母计），如 2:1:0"
        ),
    )
    text: str = Field(..., description="提示文字（去除空白后不能为空）")


class RehearsalCreateRequest(BaseModel):
    score_id: str = Field(..., description="已保存谱例的 ID（POST /scores 返回）")
    cues: list[CueInput] = Field(
        ...,
        description="提示表；至少一个，按录入顺序编号（触发时刻相同时按录入顺序发放）",
    )


class CueStatus(BaseModel):
    index: int
    position: str
    text: str
    total_ticks: int = Field(..., description="提示位置距起点的 tick 总数（PPQ 坐标）")
    status: Literal["pending", "delivered", "skipped"] = Field(
        ...,
        description=(
            "提示状态：pending=待发，delivered=已发放（不撤回、不重发），"
            "skipped=定位跳过时位于目标位置之前（回跳也不恢复）"
        ),
    )
    delivered: bool = Field(..., description="是否已发放（与 status == 'delivered' 等价）")
    trigger_seconds: Optional[float] = Field(
        None, description="已发放提示的触发秒数（按发放时所用速度图修订计算）"
    )
    revision: Optional[int] = Field(
        None, description="发放时所用的速度图修订号（与触发秒数同一次计算）"
    )
    skipped_revision: Optional[int] = Field(
        None, description="记为跳过时所用的速度图修订号；pending/delivered 时为 null"
    )


class RehearsalOut(BaseModel):
    id: str
    score_id: str
    last_elapsed_seconds: float = Field(
        ..., description="已领取到的最大累计秒数；领取进度只能前进"
    )
    origin: "TimingOrigin" = Field(
        ...,
        description=(
            "当前计时起点：每次定位更新（未定位时为 1:1:0 / 0.0s）；"
            "后续提示触发秒数 = origin_elapsed_seconds + "
            "速度图上 origin 位置到提示位置的时长"
        ),
    )
    cues: list[CueStatus]


class TimingOrigin(BaseModel):
    position: str = Field(
        ..., description="最近一次定位的目标乐谱位置（未定位时为 1:1:0）"
    )
    total_ticks: int = Field(..., description="计时起点位置的 tick 坐标")
    elapsed_seconds: float = Field(
        ..., description="定位时提交、作为新起点的累计秒数（未定位时为 0.0）"
    )


class ClaimRequest(BaseModel):
    elapsed_seconds: float = Field(
        ...,
        description=(
            "从排练开始累计的秒数（非负、有限）；只能前进，"
            "早于已领取进度返回 409 且状态不变"
        ),
    )


class ClaimedCue(BaseModel):
    index: int
    position: str
    text: str
    trigger_seconds: float = Field(
        ..., description="按本次所用速度图修订计算的触发秒数"
    )
    revision: int = Field(..., description="本次计算到点所用的速度图修订号")


class ClaimResponse(BaseModel):
    id: str
    elapsed_seconds: float
    revision: int = Field(
        ..., description="本次领取计算到点所用的速度图修订号（与发放记录一致）"
    )
    cues: list[ClaimedCue] = Field(
        ..., description="本次新发放的到点提示，按触发秒数及录入顺序排列"
    )


class LocateRequest(BaseModel):
    elapsed_seconds: float = Field(
        ...,
        description=(
            "当前已领取的累计秒数（非负、有限）；必须与排练当前进度"
            "完全相等，否则返回 409 且状态不变"
        ),
    )
    position: str = Field(
        ...,
        description=(
            "定位目标乐谱位置 '小节:拍:tick'（拍按当前拍号分母计），"
            "如 4:1:0"
        ),
    )


class SkippedCue(BaseModel):
    index: int
    position: str
    text: str
    total_ticks: int
    revision: int = Field(..., description="定位（记为跳过）时谱例的速度图修订号")


class LocateResponse(BaseModel):
    id: str
    elapsed_seconds: float = Field(
        ..., description="本次定位提交并作为新计时起点的累计秒数"
    )
    revision: int = Field(
        ..., description="定位时谱例当前已提交的速度图修订号（与跳过记录一致）"
    )
    origin: TimingOrigin
    skipped: list[SkippedCue] = Field(
        ...,
        description=(
            "本次新记为跳过的提示（此前待发且位于目标位置之前；"
            "位于目标处的提示不跳过，可在提交的同一秒领取）"
        ),
    )
    cues: list[CueStatus] = Field(..., description="定位后的全部提示状态")


RehearsalOut.model_rebuild()


def _rehearsal_not_found(rehearsal_id: str) -> JSONResponse:
    return _error_response(
        [
            {
                "loc": ["path", "rehearsal_id"],
                "msg": f"排练 {rehearsal_id} 不存在",
                "index": None,
            }
        ],
        status_code=404,
    )


def _regression(exc: ElapsedRegression) -> JSONResponse:
    # 倒退请求：409，状态不变（不推进进度、不发放任何提示）。
    detail = {
        "loc": ["body", "elapsed_seconds"],
        "msg": (
            f"领取进度只能前进：本次 {exc.elapsed}s 早于"
            f"已领取进度 {exc.last_elapsed}s"
        ),
        "index": None,
        "elapsed_seconds": exc.elapsed,
        "last_elapsed_seconds": exc.last_elapsed,
    }
    return _error_response([detail], status_code=409)


def _origin_mismatch(exc: OriginMismatch) -> JSONResponse:
    # 定位秒数不等于当前领取进度：409，起点/跳过/已发记录一律不变。
    detail = {
        "loc": ["body", "elapsed_seconds"],
        "msg": (
            f"定位秒数必须等于当前已领取进度：本次 {exc.elapsed}s，"
            f"已领取进度 {exc.last_elapsed}s"
        ),
        "index": None,
        "elapsed_seconds": exc.elapsed,
        "last_elapsed_seconds": exc.last_elapsed,
    }
    return _error_response([detail], status_code=409)


def _origin_offset_seconds(score: dict, tmap: TempoMap) -> float:
    """定位计时起点在当前速度图上的秒数（origin 默认 1:1:0，即 0.0s）。

    触发秒数统一为：提交定位秒数 + 起点 tick 到提示 tick 的速度图时长，
    即 ``origin_elapsed + seconds_at(cue) - seconds_at(origin)``。
    未定位的排练起点为 (tick 0, 0.0s)，退化为原有的“按全图从 0 开始计时”。
    """
    return (
        float(score.get("origin_elapsed", 0.0))
        - tmap.seconds_at_tick(score.get("origin_ticks", 0))
    )


def _due_cues(score: dict, pending: list[dict], elapsed: float) -> list[dict]:
    """按谱例当前已提交速度图计算未发提示的触发秒数，筛出到点项。

    触发秒数相对排练当前定位起点：origin_elapsed + 起点到提示的速度图
    时长（定位起点不变；速度图发布后待发提示自然按新修订重算）。

    返回项按（触发秒数, 录入顺序）排序。本函数作为纯计算回调在
    :meth:`Store.claim_cues` 的写事务内执行，``score`` 即该事务快照
    中的当前修订，保证计算所用修订与发放记录一致。
    """
    tmap = _build_map(
        score["ppq"], score["time_signatures"], score["tempo_points"]
    )
    offset = _origin_offset_seconds(score, tmap)
    due = []
    for c in pending:
        trigger = offset + tmap.seconds_at_tick(c["total_ticks"])
        if trigger <= elapsed:
            due.append({"cue_index": c["cue_index"], "trigger_seconds": trigger})
    due.sort(key=lambda d: (d["trigger_seconds"], d["cue_index"]))
    return due


def _skip_before_target(
    score: dict, pending: list[dict], target_ticks: int
) -> list[int]:
    """定位回调：返回待发提示中 tick 严格位于目标位置之前的下标。

    与目标同 tick 的提示不跳过（可在定位提交的同一秒领取）；
    回调只读取当前修订快照，是否跳过只取决于坐标先后，与速度无关。
    """
    return sorted(
        c["cue_index"] for c in pending if c["total_ticks"] < target_ticks
    )


def _rehearsal_out(score: dict, rehearsal: dict) -> RehearsalOut:
    """组装排练响应：补上计时起点位置字符串（tick -> 小节:拍:tick）。"""
    tmap = _build_map(
        score["ppq"], score["time_signatures"], score["tempo_points"]
    )
    origin_ticks = rehearsal.get("origin_ticks", 0)
    return RehearsalOut.model_validate(
        {
            "id": rehearsal["id"],
            "score_id": rehearsal["score_id"],
            "last_elapsed_seconds": rehearsal["last_elapsed_seconds"],
            "origin": {
                "position": str(tmap.ticks_to_mbt(origin_ticks)),
                "total_ticks": origin_ticks,
                "elapsed_seconds": rehearsal.get("origin_elapsed_seconds", 0.0),
            },
            "cues": rehearsal["cues"],
        }
    )


@app.post("/rehearsals", response_model=RehearsalOut, status_code=201)
async def create_rehearsal(req: RehearsalCreateRequest):
    """创建排练：校验各提示位置（带数组索引的 422），返回排练 ID。"""
    try:
        score = store.get_score(req.score_id)
    except ScoreNotFound:
        return _not_found(req.score_id)

    if not req.cues:
        raise TempoMapError(
            [
                {
                    "loc": ["body", "cues"],
                    "msg": "至少需要一个排练提示",
                    "index": None,
                }
            ]
        )

    # 位置校验沿用谱例创建时的 PPQ/拍号（与 /convert、锚点同一套规则）。
    tmap = _build_map(
        score["ppq"], score["time_signatures"], score["tempo_points"]
    )
    cue_rows: list[dict] = []
    cue_errors: list[dict] = []
    for i, c in enumerate(req.cues):
        base = ("body", "cues", i)
        if not c.text.strip():
            cue_errors.append(
                {
                    "loc": ["body", "cues", i, "text"],
                    "msg": "提示文字不能为空",
                    "index": i,
                }
            )
        try:
            total_ticks = tmap.mbt_to_ticks(c.position, loc=(*base, "position"))
        except TempoMapError as exc:
            for e in exc.errors:
                e["index"] = i
                cue_errors.append(e)
            continue
        cue_rows.append(
            {"position": c.position, "text": c.text, "total_ticks": total_ticks}
        )
    if cue_errors:
        raise TempoMapError(cue_errors)

    try:
        rehearsal = store.create_rehearsal(req.score_id, cue_rows)
    except ScoreNotFound:
        return _not_found(req.score_id)
    return _rehearsal_out(score, rehearsal)


@app.post("/rehearsals/{rehearsal_id}/claims", response_model=ClaimResponse)
async def claim_rehearsal_cues(rehearsal_id: str, req: ClaimRequest):
    """领取到点提示：按谱例当前已提交速度图定时，进度只前进。"""
    elapsed = req.elapsed_seconds
    if not (
        isinstance(elapsed, (int, float))
        and not isinstance(elapsed, bool)
        and math.isfinite(elapsed)
        and elapsed >= 0
    ):
        raise TempoMapError(
            [
                {
                    "loc": ["body", "elapsed_seconds"],
                    "msg": "elapsed_seconds 必须是有限的非负数",
                    "index": None,
                }
            ]
        )
    elapsed_value = float(elapsed)

    def plan(score: dict, pending: list[dict]) -> list[dict]:
        return _due_cues(score, pending, elapsed_value)

    try:
        result = store.claim_cues(rehearsal_id, elapsed_value, plan)
    except RehearsalNotFound:
        return _rehearsal_not_found(rehearsal_id)
    except ElapsedRegression as exc:
        return _regression(exc)
    except ScoreNotFound as exc:  # 防御性：谱例被删（正常不会发生）
        return _not_found(str(exc))
    return ClaimResponse.model_validate(result)


@app.post("/rehearsals/{rehearsal_id}/locate", response_model=LocateResponse)
async def locate_rehearsal(rehearsal_id: str, req: LocateRequest):
    """定位（跳转）到另一乐谱位置：提交当前已领取累计秒数与目标位置。

    目标之前的未发提示记为跳过（回跳不恢复、不重发）；计时起点更新为
    （提交秒数, 目标位置），此后提示触发秒数 = 提交秒数 + 速度图上
    目标到提示的时长。提交秒数必须等于当前领取进度，否则 409 状态不变。
    """
    elapsed = req.elapsed_seconds
    if not (
        isinstance(elapsed, (int, float))
        and not isinstance(elapsed, bool)
        and math.isfinite(elapsed)
        and elapsed >= 0
    ):
        raise TempoMapError(
            [
                {
                    "loc": ["body", "elapsed_seconds"],
                    "msg": "elapsed_seconds 必须是有限的非负数",
                    "index": None,
                }
            ]
        )
    elapsed_value = float(elapsed)

    # 位置按谱例当前 PPQ/拍号校验（速度点修订不改坐标）。先取谱例：
    # 排练不存在优先返回 404（与领取/查询一致）。
    try:
        rehearsal = store.get_rehearsal(rehearsal_id)
    except RehearsalNotFound:
        return _rehearsal_not_found(rehearsal_id)
    try:
        score = store.get_score(rehearsal["score_id"])
    except ScoreNotFound:  # 防御性：谱例被删（正常不会发生）
        return _not_found(rehearsal["score_id"])

    tmap = _build_map(
        score["ppq"], score["time_signatures"], score["tempo_points"]
    )
    target_ticks = tmap.mbt_to_ticks(
        req.position, loc=("body", "position")
    )

    try:
        result = store.locate(
            rehearsal_id, elapsed_value, target_ticks, _skip_before_target
        )
    except OriginMismatch as exc:
        return _origin_mismatch(exc)
    except ScoreNotFound as exc:  # 防御性：谱例被删（正常不会发生）
        return _not_found(str(exc))

    by_index = {c["index"]: c for c in result["rehearsal"]["cues"]}
    skipped = []
    for cue_index in result["skipped"]:
        c = by_index[cue_index]
        skipped.append(
            SkippedCue(
                index=cue_index,
                position=c["position"],
                text=c["text"],
                total_ticks=c["total_ticks"],
                revision=result["revision"],
            )
        )
    origin = _rehearsal_out(score, result["rehearsal"]).origin
    return LocateResponse(
        id=rehearsal_id,
        elapsed_seconds=elapsed_value,
        revision=result["revision"],
        origin=origin,
        skipped=skipped,
        cues=result["rehearsal"]["cues"],
    )


@app.get("/rehearsals/{rehearsal_id}", response_model=RehearsalOut)
async def get_rehearsal(rehearsal_id: str):
    """查询排练：提示列表、发放/跳过状态（触发秒数/所用修订）与计时起点。"""
    try:
        rehearsal = store.get_rehearsal(rehearsal_id)
    except RehearsalNotFound:
        return _rehearsal_not_found(rehearsal_id)
    try:
        score = store.get_score(rehearsal["score_id"])
    except ScoreNotFound:  # 防御性：谱例被删（正常不会发生）
        return _not_found(rehearsal["score_id"])
    return _rehearsal_out(score, rehearsal)
