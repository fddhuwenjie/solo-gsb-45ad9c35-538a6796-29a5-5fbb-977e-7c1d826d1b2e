"""带录音锚点的速度图修订：创建 / 预览 / 发布 / 查询 端到端测试。"""

import math
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """每个测试用独立 SQLite，并重新初始化模块级 store。"""
    db_path = tmp_path / "scores.db"
    monkeypatch.setenv("TEMPO_DB_PATH", str(db_path))

    import app.main as main
    from app.db import Store

    monkeypatch.setattr(main, "store", Store(str(db_path)))
    yield db_path


from app.main import app  # noqa: E402

client = TestClient(app)


CREATE_BODY = {
    "ppq": 480,
    "time_signatures": [],
    "tempo_points": [
        {"position": "1:1:0", "bpm": 120, "curve": "step"},
    ],
    "anchors": [
        {"position": "1:1:0", "locked": True},
        {"position": "2:1:0", "locked": False},
    ],
}

# 替换表：整体降到 60 BPM（2:1:0 从 2.0s 变 4.0s；1:1:0 恒为 0）
SLOW_POINTS = [
    {"position": "1:1:0", "bpm": 60, "curve": "step"},
]
# 替换表：整体升到 240 BPM（2:1:0 从 2.0s 变 1.0s）
FAST_POINTS = [
    {"position": "1:1:0", "bpm": 240, "curve": "step"},
]


def create_score(body=None):
    resp = client.post("/scores", json=body or CREATE_BODY)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------- #
# 创建
# ---------------------------------------------------------------------- #
def test_create_score_returns_id_revision_zero_and_anchor_seconds():
    data = create_score()
    assert isinstance(data["id"], str) and len(data["id"]) == 32
    assert data["revision"] == 0
    assert data["ppq"] == 480
    assert len(data["anchors"]) == 2

    locked, unlocked = data["anchors"]
    assert locked["index"] == 0 and locked["locked"] is True
    assert locked["position"] == "1:1:0"
    assert math.isclose(locked["original_seconds"], 0.0)
    assert math.isclose(locked["current_seconds"], 0.0)
    # 120 BPM、PPQ=480：一拍 0.5s，一 tick = 0.5/480
    assert math.isclose(locked["tick_seconds"], 0.5 / 480)

    assert unlocked["index"] == 1 and unlocked["locked"] is False
    assert unlocked["position"] == "2:1:0"
    assert unlocked["total_ticks"] == 1920
    # 一小节 4 拍 * 0.5s = 2.0s
    assert math.isclose(unlocked["original_seconds"], 2.0)
    assert math.isclose(unlocked["current_seconds"], 2.0)
    assert math.isclose(unlocked["tick_seconds"], 0.5 / 480)


def test_create_requires_at_least_one_locked_anchor():
    body = {
        **CREATE_BODY,
        "anchors": [{"position": "2:1:0", "locked": False}],
    }
    resp = client.post("/scores", json=body)
    assert resp.status_code == 422
    assert any("locked" in e["msg"] for e in resp.json()["detail"])


def test_create_empty_anchors_is_422():
    resp = client.post("/scores", json={**CREATE_BODY, "anchors": []})
    assert resp.status_code == 422


def test_create_reuses_tempo_validation():
    # 首点不在 1:1:0、BPM 非法：沿用 /convert 的 422 校验
    body = {
        **CREATE_BODY,
        "tempo_points": [{"position": "1:2:0", "bpm": -5, "curve": "step"}],
    }
    resp = client.post("/scores", json=body)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any(e.get("index") == 0 for e in detail)


def test_create_bad_anchor_position_is_422_with_index():
    body = {
        **CREATE_BODY,
        "anchors": [
            {"position": "1:1:0", "locked": True},
            {"position": "1:9:0", "locked": False},
        ],
    }
    resp = client.post("/scores", json=body)
    assert resp.status_code == 422
    assert any(e.get("index") == 1 for e in resp.json()["detail"])


# ---------------------------------------------------------------------- #
# 预览
# ---------------------------------------------------------------------- #
def test_preview_reports_original_new_offset_and_conflict_without_saving():
    score = create_score()
    sid = score["id"]
    resp = client.post(
        f"/scores/{sid}/revisions/0/preview", json={"tempo_points": SLOW_POINTS}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["expected_revision"] == 0
    assert data["current_revision"] == 0
    assert data["has_lock_conflict"] is False

    a0, a1 = data["anchors"]
    # 锁定锚点在原点：秒数恒为 0，无偏移
    assert a0["locked"] is True
    assert math.isclose(a0["original_seconds"], 0.0)
    assert math.isclose(a0["new_seconds"], 0.0)
    assert math.isclose(a0["offset_seconds"], 0.0)
    assert a0["lock_conflict"] is False
    # 未锁定锚点 2:1:0：2.0s -> 4.0s
    assert a1["locked"] is False
    assert math.isclose(a1["original_seconds"], 2.0)
    assert math.isclose(a1["new_seconds"], 4.0)
    assert math.isclose(a1["offset_seconds"], 2.0)
    assert a1["lock_conflict"] is False

    # 预览不修改已存数据：修订号仍为 0，速度图仍是 120 BPM
    stored = client.get(f"/scores/{sid}").json()
    assert stored["revision"] == 0
    assert stored["tempo_points"][0]["bpm"] == 120
    assert math.isclose(stored["anchors"][1]["current_seconds"], 2.0)


def test_preview_flags_lock_conflict_but_still_returns_200():
    # 锁定锚点放在 2:1:0（原图 2.0s）；放慢到 60 BPM -> 4.0s，偏移 2s 远超一 tick
    body = {
        **CREATE_BODY,
        "anchors": [
            {"position": "1:1:0", "locked": False},
            {"position": "2:1:0", "locked": True},
        ],
    }
    score = client.post("/scores", json=body).json()
    resp = client.post(
        f"/scores/{score['id']}/revisions/0/preview",
        json={"tempo_points": SLOW_POINTS},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["has_lock_conflict"] is True
    locked = data["anchors"][1]
    assert locked["lock_conflict"] is True
    assert math.isclose(locked["offset_seconds"], 2.0)


def test_preview_stale_revision_is_409():
    score = create_score()
    client.post(
        f"/scores/{score['id']}/revisions/0/publish",
        json={"tempo_points": SLOW_POINTS},
    )
    resp = client.post(
        f"/scores/{score['id']}/revisions/0/preview",
        json={"tempo_points": FAST_POINTS},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"][0]
    assert detail["expected_revision"] == 0
    assert detail["current_revision"] == 1


def test_preview_invalid_replacement_is_422():
    score = create_score()
    resp = client.post(
        f"/scores/{score['id']}/revisions/0/preview",
        json={"tempo_points": [{"position": "2:1:0", "bpm": 90, "curve": "step"}]},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------- #
# 发布
# ---------------------------------------------------------------------- #
def test_publish_updates_map_unlocked_anchors_and_revision_keeps_locked_seconds():
    score = create_score()
    sid = score["id"]
    resp = client.post(
        f"/scores/{sid}/revisions/0/publish", json={"tempo_points": SLOW_POINTS}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["revision"] == 1
    assert data["tempo_points"][0]["bpm"] == 60

    locked, unlocked = data["anchors"]
    # 锁定秒数保持创建时冻结的值（0.0），tick_seconds 阈值也不变
    assert locked["locked"] is True
    assert math.isclose(locked["original_seconds"], 0.0)
    assert math.isclose(locked["current_seconds"], 0.0)
    assert math.isclose(locked["tick_seconds"], 0.5 / 480)
    # 未锁定锚点秒数随新图更新：2.0s -> 4.0s
    assert unlocked["locked"] is False
    assert math.isclose(unlocked["original_seconds"], 2.0)
    assert math.isclose(unlocked["current_seconds"], 4.0)

    # 查询返回同一版本（修订号与速度图、锚点一致）
    stored = client.get(f"/scores/{sid}").json()
    assert stored["revision"] == 1
    assert stored["tempo_points"][0]["bpm"] == 60
    assert math.isclose(stored["anchors"][1]["current_seconds"], 4.0)
    assert math.isclose(stored["anchors"][0]["current_seconds"], 0.0)


def test_publish_lock_conflict_is_422_and_data_unchanged():
    body = {
        **CREATE_BODY,
        "anchors": [
            {"position": "1:1:0", "locked": False},
            {"position": "2:1:0", "locked": True},
        ],
    }
    score = client.post("/scores", json=body).json()
    sid = score["id"]

    resp = client.post(
        f"/scores/{sid}/revisions/0/publish", json={"tempo_points": SLOW_POINTS}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any(e.get("index") == 1 for e in detail)
    assert any("锁定锚点" in e["msg"] for e in detail)

    # 数据不变：修订号 0、原图 120 BPM、锁定锚点秒数仍为创建时的 2.0s
    stored = client.get(f"/scores/{sid}").json()
    assert stored["revision"] == 0
    assert stored["tempo_points"][0]["bpm"] == 120
    assert math.isclose(stored["anchors"][1]["current_seconds"], 2.0)
    assert math.isclose(stored["anchors"][1]["original_seconds"], 2.0)


def test_publish_stale_expected_revision_after_another_publish_is_409():
    score = create_score()
    sid = score["id"]
    r1 = client.post(
        f"/scores/{sid}/revisions/0/publish", json={"tempo_points": SLOW_POINTS}
    )
    assert r1.status_code == 200

    # 旧预期修订号 0（即便预览过 rev0）再次发布 -> 409，不能覆盖新版本
    r2 = client.post(
        f"/scores/{sid}/revisions/0/publish", json={"tempo_points": FAST_POINTS}
    )
    assert r2.status_code == 409
    assert r2.json()["detail"][0]["current_revision"] == 1

    # 当前版本仍是 rev1 的 60 BPM
    stored = client.get(f"/scores/{sid}").json()
    assert stored["revision"] == 1
    assert stored["tempo_points"][0]["bpm"] == 60

    # 用最新预期修订号 1 可以继续发布
    r3 = client.post(
        f"/scores/{sid}/revisions/1/publish", json={"tempo_points": FAST_POINTS}
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["revision"] == 2
    assert r3.json()["tempo_points"][0]["bpm"] == 240


def test_publish_threshold_exactly_one_tick_is_allowed():
    # 构造偏移恰好等于“录入时一 tick 时长”的边界：严格“超过”才 422。
    # 原图锚点 2:1:0 = 2.0s，一 tick = 0.5/480 s。
    # 要让新秒数 = 2.0 + 0.5/480，即 1921 tick 处的时间，
    # 取新 BPM 使一小节(1920 tick)时长 = 2.0 + 0.5/480：
    # new_bpm = 240 / (2.0 + 0.5/480)
    new_bar_seconds = 2.0 + 0.5 / 480
    new_bpm = 240.0 / new_bar_seconds
    body = {
        **CREATE_BODY,
        "anchors": [{"position": "2:1:0", "locked": True}],
    }
    score = client.post("/scores", json=body).json()
    resp = client.post(
        f"/scores/{score['id']}/revisions/0/publish",
        json={"tempo_points": [{"position": "1:1:0", "bpm": new_bpm, "curve": "step"}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 1
    # 锁定秒数仍冻结为 2.0
    assert math.isclose(
        resp.json()["anchors"][0]["current_seconds"], 2.0
    )


def test_publish_just_beyond_one_tick_is_422():
    # 比一 tick 再多偏移一点 -> 严格超过 -> 422
    new_bar_seconds = 2.0 + 0.5 / 480 + 1e-5
    new_bpm = 240.0 / new_bar_seconds
    body = {
        **CREATE_BODY,
        "anchors": [{"position": "2:1:0", "locked": True}],
    }
    score = client.post("/scores", json=body).json()
    resp = client.post(
        f"/scores/{score['id']}/revisions/0/publish",
        json={"tempo_points": [{"position": "1:1:0", "bpm": new_bpm, "curve": "step"}]},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["index"] == 0


def test_publish_recomputes_from_current_revision():
    # 连续发布两次：第二次的“原秒数”比对基准始终是创建时冻结值，
    # 且新图按当前修订（rev1 的图）之后替换整表。
    score = create_score()
    sid = score["id"]
    client.post(
        f"/scores/{sid}/revisions/0/publish", json={"tempo_points": SLOW_POINTS}
    )
    resp = client.post(
        f"/scores/{sid}/revisions/1/publish", json={"tempo_points": FAST_POINTS}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["revision"] == 2
    # 未锁定锚点 2:1:0 在 240 BPM 下 = 1.0s（不是相对 rev1 的增量）
    assert math.isclose(data["anchors"][1]["current_seconds"], 1.0)
    assert math.isclose(data["anchors"][1]["original_seconds"], 2.0)


def test_publish_invalid_replacement_is_422_and_unchanged():
    score = create_score()
    sid = score["id"]
    resp = client.post(
        f"/scores/{sid}/revisions/0/publish",
        json={
            "tempo_points": [
                {"position": "1:1:0", "bpm": 120, "curve": "step"},
                {"position": "1:1:0", "bpm": 90, "curve": "step"},
            ]
        },
    )
    assert resp.status_code == 422
    stored = client.get(f"/scores/{sid}").json()
    assert stored["revision"] == 0
    assert len(stored["tempo_points"]) == 1


# ---------------------------------------------------------------------- #
# 查询 / 404
# ---------------------------------------------------------------------- #
def test_get_score_unknown_is_404():
    assert client.get("/scores/deadbeef").status_code == 404
    assert (
        client.post(
            "/scores/deadbeef/revisions/0/preview",
            json={"tempo_points": SLOW_POINTS},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/scores/deadbeef/revisions/0/publish",
            json={"tempo_points": SLOW_POINTS},
        ).status_code
        == 404
    )


def test_get_score_returns_same_version_map_and_anchors():
    score = create_score()
    sid = score["id"]
    client.post(
        f"/scores/{sid}/revisions/0/publish", json={"tempo_points": SLOW_POINTS}
    )
    got = client.get(f"/scores/{sid}").json()
    assert got["id"] == sid and got["revision"] == 1
    # 速度图与锚点取自同一版本快照
    assert got["tempo_points"][0]["bpm"] == 60
    assert math.isclose(got["anchors"][0]["current_seconds"], 0.0)
    assert math.isclose(got["anchors"][1]["current_seconds"], 4.0)


# ---------------------------------------------------------------------- #
# SQLite 持久化：新 Store 实例（模拟 Compose 重启）后仍可读
# ---------------------------------------------------------------------- #
def test_persistence_across_store_restart(fresh_db):
    db_path = fresh_db
    score = create_score()
    sid = score["id"]
    client.post(
        f"/scores/{sid}/revisions/0/publish", json={"tempo_points": SLOW_POINTS}
    )

    # 重新打开同一文件（不删除卷的 Compose 重启等价场景）
    from app.db import Store

    reopened = Store(str(db_path))
    stored = reopened.get_score(sid)
    assert stored["revision"] == 1
    assert stored["tempo_points"][0]["bpm"] == 60
    assert len(stored["anchors"]) == 2
    assert math.isclose(stored["anchors"][1]["current_seconds"], 4.0)
    # 锁定锚点的冻结秒数与一 tick 阈值都还在
    assert math.isclose(stored["anchors"][0]["original_seconds"], 0.0)
    assert math.isclose(stored["anchors"][0]["tick_seconds"], 0.5 / 480)


# ---------------------------------------------------------------------- #
# 读写交错：GET 不得跨修订拼接（速度点/修订号与全部锚点须取自同一快照）
# ---------------------------------------------------------------------- #
def _publish_from_other_connection(db_path, sid, expected_revision, points):
    """用独立 Store 连接（同一 SQLite 文件）真实提交一次发布。

    绕过 TestClient 门户线程，直接走发布代码路径（BEGIN IMMEDIATE +
    乐观锁 + 提交），以便确定性地插到 GET 的两次读取之间。
    """
    from app.db import Store
    from app.main import _build_map, _evaluate_replacement

    writer = Store(str(db_path))
    score = writer.get_score(sid)
    candidate_map = _build_map(score["ppq"], score["time_signatures"], points)
    evaluated = _evaluate_replacement(
        candidate_map, score["anchors"], raise_on_conflict=True
    )
    return writer.publish_revision(sid, expected_revision, points, evaluated)


def _assert_coherent_revision(data, *, revision, bpm, unlocked_seconds):
    """速度点、修订号与全部锚点必须描述同一次修订，不得拼接。"""
    assert data["revision"] == revision
    assert data["tempo_points"][0]["bpm"] == bpm
    assert math.isclose(data["anchors"][0]["current_seconds"], 0.0)  # 锁定秒数
    assert math.isclose(data["anchors"][1]["current_seconds"], unlocked_seconds)


def test_get_does_not_splice_across_concurrent_publish(monkeypatch, fresh_db):
    """发布恰好提交在 GET 的“谱例行已读、锚点行未读”之间。

    修复前两条 SELECT 各自取最新快照：旧修订号 0 + 旧速度点 120 BPM，
    却配上新修订未锁定锚点的 4.0s（新图 60 BPM）。修复后锚点仍取自
    读事务开始时的快照，返回完整的旧修订。
    """
    db_path = fresh_db
    sid = create_score()["id"]
    published = []

    from app.db import Store

    real_read_anchor_rows = Store._read_anchor_rows

    def read_anchor_rows_with_publish_then_read(self, conn, score_id, *args, **kwargs):
        # 此刻谱例行（旧修订）已在同一读事务内读出；在锚点读取前，
        # 另一连接完整提交 rev0 -> rev1（120 BPM -> 60 BPM）。
        # 先置标志再发布：发布辅助内部自身的 get_score 也走本钩子。
        if score_id == sid and not published:
            published.append(True)
            result = _publish_from_other_connection(db_path, sid, 0, SLOW_POINTS)
            published[0] = result
        return real_read_anchor_rows(self, conn, score_id, *args, **kwargs)

    monkeypatch.setattr(Store, "_read_anchor_rows", read_anchor_rows_with_publish_then_read)

    got = client.get(f"/scores/{sid}")
    assert got.status_code == 200, got.text
    data = got.json()
    # 旧快照：revision=0、120 BPM，未锁定锚点必须仍是旧图的 2.0s，
    # 绝不能是新图 60 BPM 的 4.0s（跨修订拼接）。
    _assert_coherent_revision(data, revision=0, bpm=120, unlocked_seconds=2.0)
    assert len(published) == 1

    # 下一次查询读到完整的新修订
    new = client.get(f"/scores/{sid}").json()
    _assert_coherent_revision(new, revision=1, bpm=60, unlocked_seconds=4.0)


def test_get_publish_between_row_and_anchors_never_splices_fast(monkeypatch, fresh_db):
    """同一交错点，发布方向改为 120 -> 240 BPM：旧快照里锚点保持 2.0s。"""
    db_path = fresh_db
    sid = create_score()["id"]
    fired = {"done": False}

    from app.db import Store

    real_read_anchor_rows = Store._read_anchor_rows

    def read_anchor_rows(self, conn, score_id, *args, **kwargs):
        if score_id == sid and not fired["done"]:
            fired["done"] = True
            _publish_from_other_connection(db_path, sid, 0, FAST_POINTS)
        return real_read_anchor_rows(self, conn, score_id, *args, **kwargs)

    monkeypatch.setattr(Store, "_read_anchor_rows", read_anchor_rows)

    data = client.get(f"/scores/{sid}").json()
    _assert_coherent_revision(data, revision=0, bpm=120, unlocked_seconds=2.0)

    new = client.get(f"/scores/{sid}").json()
    _assert_coherent_revision(new, revision=1, bpm=240, unlocked_seconds=1.0)


def test_get_publish_completes_before_anchor_snapshot_yields_old_or_new(monkeypatch, fresh_db):
    """发布在“谱例行读取之前”已提交时，查询应返回完整新修订（另一允许结果）。"""
    db_path = fresh_db
    sid = create_score()["id"]
    fired = {"done": False}

    from app.db import Store

    real_read_score_row = Store._read_score_row

    def read_score_row(self, conn, score_id, *args, **kwargs):
        # 只在 GET 自己的首次读（发布尚未发生）前插入发布；
        # 发布辅助内部 get_score 的重读必须放行，否则无限递归。
        if score_id == sid and not fired["done"]:
            fired["done"] = True
            _publish_from_other_connection(db_path, sid, 0, SLOW_POINTS)
        return real_read_score_row(self, conn, score_id, *args, **kwargs)

    monkeypatch.setattr(Store, "_read_score_row", read_score_row)

    data = client.get(f"/scores/{sid}").json()
    # 读事务的第一条 SELECT 在发布提交之后才取快照 -> 完整新修订
    _assert_coherent_revision(data, revision=1, bpm=60, unlocked_seconds=4.0)

