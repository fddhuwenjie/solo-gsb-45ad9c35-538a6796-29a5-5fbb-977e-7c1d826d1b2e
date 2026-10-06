"""排练提示：创建排练 / 领取到点提示 / 进度与并发 端到端测试。"""

import math
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """每个测试用独立 SQLite，并重新初始化模块级 store。"""
    db_path = tmp_path / "rehearsals.db"
    monkeypatch.setenv("TEMPO_DB_PATH", str(db_path))

    import app.main as main
    from app.db import Store

    monkeypatch.setattr(main, "store", Store(str(db_path)))
    yield db_path


from app.main import app  # noqa: E402

client = TestClient(app)

# 120 BPM、4/4、PPQ=480：一拍 0.5s、一小节 2.0s
# 1:1:0 -> 0.0s，1:2:0 -> 0.5s，2:1:0 -> 2.0s，3:1:0 -> 4.0s
SCORE_BODY = {
    "ppq": 480,
    "time_signatures": [],
    "tempo_points": [{"position": "1:1:0", "bpm": 120, "curve": "step"}],
    "anchors": [{"position": "1:1:0", "locked": True}],
}
# 替换表：整体降到 60 BPM（所有触发秒数翻倍）
SLOW_POINTS = [{"position": "1:1:0", "bpm": 60, "curve": "step"}]
# 替换表：整体升到 240 BPM（所有触发秒数减半）
FAST_POINTS = [{"position": "1:1:0", "bpm": 240, "curve": "step"}]


def create_score():
    resp = client.post("/scores", json=SCORE_BODY)
    assert resp.status_code == 201, resp.text
    return resp.json()


def create_rehearsal(score_id, cues):
    resp = client.post("/rehearsals", json={"score_id": score_id, "cues": cues})
    assert resp.status_code == 201, resp.text
    return resp.json()


def claim(rid, elapsed):
    return client.post(f"/rehearsals/{rid}/claims", json={"elapsed_seconds": elapsed})


# ---------------------------------------------------------------------- #
# 创建排练
# ---------------------------------------------------------------------- #
def test_create_rehearsal_validates_positions_and_returns_id():
    score = create_score()
    r = create_rehearsal(
        score["id"],
        [
            {"position": "2:1:0", "text": "进副歌"},
            {"position": "1:2:0", "text": "抬头"},
        ],
    )
    assert isinstance(r["id"], str) and len(r["id"]) == 32
    assert r["score_id"] == score["id"]
    assert r["last_elapsed_seconds"] == 0.0

    c0, c1 = r["cues"]
    assert c0["index"] == 0 and c0["position"] == "2:1:0"
    assert c0["total_ticks"] == 1920
    assert c0["delivered"] is False
    assert c0["trigger_seconds"] is None and c0["revision"] is None
    assert c1["index"] == 1 and c1["total_ticks"] == 480


def test_create_rehearsal_unknown_score_is_404():
    resp = client.post(
        "/rehearsals",
        json={"score_id": "deadbeef", "cues": [{"position": "1:1:0", "text": "x"}]},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"][0]["loc"] == ["path", "score_id"]


def test_create_rehearsal_bad_position_and_empty_text_are_422_with_index():
    score = create_score()
    resp = client.post(
        "/rehearsals",
        json={
            "score_id": score["id"],
            "cues": [
                {"position": "1:9:0", "text": "ok"},  # 拍超出 4/4 分子
                {"position": "2:1:0", "text": "   "},  # 空白文字
                {"position": "not-a-position", "text": "ok"},  # 格式非法
            ],
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any(e["index"] == 0 and "position" in e["loc"] for e in detail)
    assert any(e["index"] == 1 and "text" in e["loc"] for e in detail)
    assert any(e["index"] == 2 and "position" in e["loc"] for e in detail)
    # 校验失败不落库
    assert client.get(f"/scores/{score['id']}").json()["revision"] == 0


def test_create_rehearsal_requires_at_least_one_cue():
    score = create_score()
    resp = client.post(
        "/rehearsals", json={"score_id": score["id"], "cues": []}
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------- #
# 领取：到点判定与排序
# ---------------------------------------------------------------------- #
def test_claim_delivers_due_cues_ordered_by_trigger_then_entry_order():
    score = create_score()
    r = create_rehearsal(
        score["id"],
        [
            {"position": "3:1:0", "text": "C"},  # 4.0s
            {"position": "1:1:0", "text": "A"},  # 0.0s
            {"position": "2:1:0", "text": "B1"},  # 2.0s
            {"position": "2:1:0", "text": "B2"},  # 2.0s（同刻，按录入顺序）
        ],
    )
    resp = claim(r["id"], 2.0)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == r["id"]
    assert data["elapsed_seconds"] == 2.0
    assert data["revision"] == 0
    # C(4.0s) 未到点；其余按触发秒数、同刻按录入顺序
    assert [c["text"] for c in data["cues"]] == ["A", "B1", "B2"]
    triggers = [c["trigger_seconds"] for c in data["cues"]]
    assert triggers == sorted(triggers)
    assert all(c["revision"] == 0 for c in data["cues"])
    # 位置与索引回显
    assert data["cues"][1]["position"] == "2:1:0"
    assert data["cues"][1]["index"] == 2

    # 推进到 4.0s：补发 C
    resp2 = claim(r["id"], 4.0)
    assert [c["text"] for c in resp2.json()["cues"]] == ["C"]


def test_claim_at_exact_trigger_second_is_due():
    score = create_score()
    r = create_rehearsal(score["id"], [{"position": "2:1:0", "text": "B"}])
    data = claim(r["id"], 2.0).json()
    assert len(data["cues"]) == 1
    assert math.isclose(data["cues"][0]["trigger_seconds"], 2.0)


def test_claim_rejects_negative_or_nonfinite_elapsed():
    score = create_score()
    r = create_rehearsal(score["id"], [{"position": "1:1:0", "text": "A"}])
    assert claim(r["id"], -0.5).status_code == 422
    # NaN/Infinity 不是标准 JSON，但 Python json.loads 会接受：直接发原始请求体
    for raw in ("NaN", "Infinity", "-Infinity"):
        resp = client.post(
            f"/rehearsals/{r['id']}/claims",
            content=f'{{"elapsed_seconds": {raw}}}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422, raw
    # 状态不变：进度仍为 0，提示未发放
    got = client.get(f"/rehearsals/{r['id']}").json()
    assert got["last_elapsed_seconds"] == 0.0
    assert got["cues"][0]["delivered"] is False


def test_unknown_rehearsal_is_404():
    assert client.get("/rehearsals/deadbeef").status_code == 404
    resp = claim("deadbeef", 1.0)
    assert resp.status_code == 404
    assert resp.json()["detail"][0]["loc"] == ["path", "rehearsal_id"]


# ---------------------------------------------------------------------- #
# 进度只前进：重复领取不重发，倒退报错且不改状态
# ---------------------------------------------------------------------- #
def test_repeat_claim_at_same_elapsed_does_not_redeliver():
    score = create_score()
    r = create_rehearsal(score["id"], [{"position": "2:1:0", "text": "B"}])
    first = claim(r["id"], 2.0).json()
    assert len(first["cues"]) == 1

    second = claim(r["id"], 2.0).json()
    assert second["cues"] == []

    got = client.get(f"/rehearsals/{r['id']}").json()
    assert got["last_elapsed_seconds"] == 2.0
    cue = got["cues"][0]
    assert cue["delivered"] is True
    assert math.isclose(cue["trigger_seconds"], 2.0)
    assert cue["revision"] == 0


def test_regressing_claim_is_409_and_state_unchanged():
    score = create_score()
    r = create_rehearsal(
        score["id"],
        [
            {"position": "2:1:0", "text": "B"},
            {"position": "3:1:0", "text": "C"},
        ],
    )
    claim(r["id"], 2.0)  # 发放 B，进度推进到 2.0

    resp = claim(r["id"], 1.0)
    assert resp.status_code == 409
    detail = resp.json()["detail"][0]
    assert detail["elapsed_seconds"] == 1.0
    assert detail["last_elapsed_seconds"] == 2.0

    # 状态不变：进度仍是 2.0，C 仍未发放；之后按 4.0 可正常领取 C
    got = client.get(f"/rehearsals/{r['id']}").json()
    assert got["last_elapsed_seconds"] == 2.0
    assert got["cues"][1]["delivered"] is False
    ok = claim(r["id"], 4.0).json()
    assert [c["text"] for c in ok["cues"]] == ["C"]


# ---------------------------------------------------------------------- #
# 发布与领取交错：已发不撤回，未发按新修订重新定时
# ---------------------------------------------------------------------- #
def test_publish_retimes_pending_cues_but_keeps_delivered():
    score = create_score()
    sid = score["id"]
    r = create_rehearsal(
        sid,
        [
            {"position": "2:1:0", "text": "B"},  # 120 BPM: 2.0s
            {"position": "3:1:0", "text": "C"},  # 120 BPM: 4.0s
        ],
    )
    rid = r["id"]

    # 先领取 B（rev0，触发 2.0s）
    first = claim(rid, 2.0).json()
    assert [c["text"] for c in first["cues"]] == ["B"]
    assert first["cues"][0]["revision"] == 0

    # 发布 60 BPM：未发提示 C 重新定时为 8.0s
    pub = client.post(
        f"/scores/{sid}/revisions/0/publish", json={"tempo_points": SLOW_POINTS}
    )
    assert pub.status_code == 200

    # 已发提示不撤回、不重发；4.0s 时 C 在新图下未到点（8.0s）
    mid = claim(rid, 4.0).json()
    assert mid["cues"] == []
    assert mid["revision"] == 1  # 本次计算所用修订与谱例当前修订一致

    # 到 8.0s 领取 C，发放记录使用新修订号
    late = claim(rid, 8.0).json()
    assert [c["text"] for c in late["cues"]] == ["C"]
    cue = late["cues"][0]
    assert math.isclose(cue["trigger_seconds"], 8.0)
    assert cue["revision"] == 1

    # 查询：B 保持旧修订的发放记录，C 是新的
    got = client.get(f"/rehearsals/{rid}").json()
    b, c = got["cues"]
    assert b["delivered"] is True
    assert math.isclose(b["trigger_seconds"], 2.0) and b["revision"] == 0
    assert c["delivered"] is True
    assert math.isclose(c["trigger_seconds"], 8.0) and c["revision"] == 1


def test_same_elapsed_claim_after_publish_delivers_newly_due_cue():
    """同一时刻重复领取不重发；发布把未发提示提前到进度内时应补发。"""
    score = create_score()
    sid = score["id"]
    r = create_rehearsal(sid, [{"position": "3:1:0", "text": "C"}])  # 4.0s @120
    rid = r["id"]
    assert claim(rid, 2.0).json()["cues"] == []  # 未到点，进度推进到 2.0

    # 发布 240 BPM：C 重新定时为 2.0s，恰好在已领取进度内
    pub = client.post(
        f"/scores/{sid}/revisions/0/publish", json={"tempo_points": FAST_POINTS}
    )
    assert pub.status_code == 200

    again = claim(rid, 2.0).json()  # 同一时刻：未发过的 C 现在到点
    assert [c["text"] for c in again["cues"]] == ["C"]
    assert again["cues"][0]["revision"] == 1
    assert math.isclose(again["cues"][0]["trigger_seconds"], 2.0)


# ---------------------------------------------------------------------- #
# 并发：同一提示最多发放一次
# ---------------------------------------------------------------------- #
def test_concurrent_claims_deliver_each_cue_at_most_once(fresh_db):
    """多个独立 Store 连接（各自线程）同时领取：合并结果无重复、无遗漏。"""
    from app.db import Store
    from app.main import _due_cues

    score = create_score()
    r = create_rehearsal(
        score["id"],
        [{"position": f"{m}:1:0", "text": f"cue{m}"} for m in range(1, 6)],
    )
    rid = r["id"]
    db_path = fresh_db

    n_workers = 4
    stores = [Store(str(db_path)) for _ in range(n_workers)]  # 独立连接
    barrier = threading.Barrier(n_workers)
    results: list[list[int]] = []
    errors: list[Exception] = []

    def worker(store):
        try:
            barrier.wait(timeout=10)
            res = store.claim_cues(
                rid, 1000.0, lambda s, p: _due_cues(s, p, 1000.0)
            )
            results.append([c["index"] for c in res["cues"]])
        except Exception as exc:  # pragma: no cover - 失败时下面断言暴露
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    delivered = sorted(i for batch in results for i in batch)
    assert delivered == [0, 1, 2, 3, 4]  # 每条提示恰好发放一次

    got = client.get(f"/rehearsals/{rid}").json()
    assert all(c["delivered"] for c in got["cues"])
    assert got["last_elapsed_seconds"] == 1000.0


# ---------------------------------------------------------------------- #
# SQLite 持久化：新 Store 实例（模拟 Compose 重启）后仍可继续领取
# ---------------------------------------------------------------------- #
def test_rehearsal_and_deliveries_survive_store_restart(fresh_db, monkeypatch):
    db_path = fresh_db
    score = create_score()
    r = create_rehearsal(
        score["id"],
        [
            {"position": "2:1:0", "text": "B"},
            {"position": "3:1:0", "text": "C"},
        ],
    )
    rid = r["id"]
    assert len(claim(rid, 2.0).json()["cues"]) == 1  # B 已发，进度 2.0

    # 重新打开同一文件（不删除卷的 Compose 重启等价场景）
    import app.main as main
    from app.db import Store

    monkeypatch.setattr(main, "store", Store(str(db_path)))

    # 同一时刻重复领取：不重发 B
    assert claim(rid, 2.0).json()["cues"] == []
    # 倒退仍报错且不改状态
    assert claim(rid, 1.0).status_code == 409
    # 继续前进可领取 C
    got = claim(rid, 4.0).json()
    assert [c["text"] for c in got["cues"]] == ["C"]

    detail = client.get(f"/rehearsals/{rid}").json()
    assert detail["last_elapsed_seconds"] == 4.0
    assert all(c["delivered"] for c in detail["cues"])
    assert math.isclose(detail["cues"][0]["trigger_seconds"], 2.0)
    assert detail["cues"][0]["revision"] == 0
