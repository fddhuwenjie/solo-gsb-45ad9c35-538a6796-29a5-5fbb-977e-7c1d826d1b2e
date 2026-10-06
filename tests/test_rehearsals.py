"""排练提示：创建排练 / 领取到点提示 / 定位跳转 / 进度与并发 端到端测试。"""

import json
import math
import sqlite3
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


def locate(rid, elapsed, position):
    return client.post(
        f"/rehearsals/{rid}/locate",
        json={"elapsed_seconds": elapsed, "position": position},
    )


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


# ---------------------------------------------------------------------- #
# 定位（跳转）：跳过目标前提示、重置计时起点
# ---------------------------------------------------------------------- #
def test_rehearsal_query_distinguishes_pending_delivered_skipped_and_origin():
    score = create_score()
    r = create_rehearsal(
        score["id"],
        [{"position": "2:1:0", "text": "B"}],
    )
    got = client.get(f"/rehearsals/{r['id']}").json()
    # 未定位：计时起点 1:1:0 / 0.0s
    assert got["origin"] == {
        "position": "1:1:0",
        "total_ticks": 0,
        "elapsed_seconds": 0.0,
    }
    cue = got["cues"][0]
    assert cue["status"] == "pending"
    assert cue["delivered"] is False
    assert cue["trigger_seconds"] is None and cue["revision"] is None
    assert cue["skipped_revision"] is None


def test_locate_marks_pending_cues_before_target_skipped():
    # B=2:1:0(2.0s)、C=3:1:0(4.0s)、E=5:1:0(8.0s)
    score = create_score()
    r = create_rehearsal(
        score["id"],
        [
            {"position": "2:1:0", "text": "B"},
            {"position": "3:1:0", "text": "C"},
            {"position": "5:1:0", "text": "E"},
        ],
    )
    rid = r["id"]
    claim(rid, 2.0)  # B 已发，进度 2.0

    # 跳到 4:1:0（绝对 6.0s），提交累计秒数 2.0
    resp = locate(rid, 2.0, "4:1:0")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["elapsed_seconds"] == 2.0
    assert data["revision"] == 0
    assert data["origin"] == {
        "position": "4:1:0",
        "total_ticks": 5760,
        "elapsed_seconds": 2.0,
    }
    # 只有目标之前的待发 C 被跳过；B 已发不动；E 在目标之后仍待发
    assert [s["text"] for s in data["skipped"]] == ["C"]
    assert data["skipped"][0]["revision"] == 0
    assert data["skipped"][0]["total_ticks"] == 3840
    statuses = {c["text"]: c["status"] for c in data["cues"]}
    assert statuses == {"B": "delivered", "C": "skipped", "E": "pending"}
    c_skipped = next(c for c in data["cues"] if c["text"] == "C")
    assert c_skipped["delivered"] is False
    assert c_skipped["skipped_revision"] == 0
    assert c_skipped["trigger_seconds"] is None and c_skipped["revision"] is None

    # 查询同样可区分三种状态并带回定位起点
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["origin"]["position"] == "4:1:0"
    assert [c["status"] for c in got["cues"]] == [
        "delivered",
        "skipped",
        "pending",
    ]
    assert got["last_elapsed_seconds"] == 2.0


def test_cue_at_target_position_can_be_claimed_at_same_second():
    # 目标处（tick 相等）的提示不跳过：4:1:0 绝对 6.0s，定位秒数 2.0，
    # 故触发秒数恰为 2.0，可在同一秒领取。
    score = create_score()
    r = create_rehearsal(
        score["id"],
        [
            {"position": "3:1:0", "text": "C"},  # 4.0s < 目标 6.0s -> 跳过
            {"position": "4:1:0", "text": "T"},  # 目标处 -> 触发 2.0
        ],
    )
    rid = r["id"]
    claim(rid, 2.0)
    data = locate(rid, 2.0, "4:1:0").json()
    assert [s["text"] for s in data["skipped"]] == ["C"]
    at_target = next(c for c in data["cues"] if c["text"] == "T")
    assert at_target["status"] == "pending"

    same_second = claim(rid, 2.0).json()
    assert [c["text"] for c in same_second["cues"]] == ["T"]
    assert math.isclose(same_second["cues"][0]["trigger_seconds"], 2.0)
    assert same_second["cues"][0]["revision"] == 0


def test_after_locate_triggers_are_origin_elapsed_plus_map_duration():
    # 先领取 B(2:1:0, 2.0s)，进度 2.0；跳到 4:1:0（绝对 6.0s）并提交
    # 2.0。E(5:1:0) 绝对 8.0s，与目标相距 2.0s -> 触发秒数 4.0
    # （而不是全图的 8.0s）。
    score = create_score()
    r = create_rehearsal(
        score["id"],
        [
            {"position": "2:1:0", "text": "B"},
            {"position": "5:1:0", "text": "E"},
        ],
    )
    rid = r["id"]
    claim(rid, 2.0)
    locate(rid, 2.0, "4:1:0")
    assert claim(rid, 3.999).json()["cues"] == []  # 未到点
    due = claim(rid, 4.0).json()
    assert [c["text"] for c in due["cues"]] == ["E"]
    assert math.isclose(due["cues"][0]["trigger_seconds"], 4.0)

    # 进度仍只前进：定位后倒退领取依旧 409
    assert claim(rid, 3.0).status_code == 409


def test_forward_locate_relabels_pending_cues_relative_to_new_origin():
    # 未做任何领取（进度 0）直接跳到 3:1:0（绝对 4.0s）：
    # 之前的 B(2.0s) 跳过；E(5:1:0, 8.0s) 触发 = 0 + (8-4) = 4.0s。
    score = create_score()
    r = create_rehearsal(
        score["id"],
        [
            {"position": "2:1:0", "text": "B"},
            {"position": "5:1:0", "text": "E"},
        ],
    )
    rid = r["id"]
    data = locate(rid, 0.0, "3:1:0").json()
    assert [s["text"] for s in data["skipped"]] == ["B"]
    assert data["origin"]["total_ticks"] == 3840
    due = claim(rid, 4.0).json()
    assert [c["text"] for c in due["cues"]] == ["E"]
    assert math.isclose(due["cues"][0]["trigger_seconds"], 4.0)


def test_backward_locate_does_not_redeliver_or_restore():
    score = create_score()
    r = create_rehearsal(
        score["id"],
        [
            {"position": "2:1:0", "text": "B"},  # 先发
            {"position": "3:1:0", "text": "C"},  # 前跳时跳过
            {"position": "5:1:0", "text": "E"},  # 之后发
        ],
    )
    rid = r["id"]
    assert [c["text"] for c in claim(rid, 2.0).json()["cues"]] == ["B"]
    locate(rid, 2.0, "4:1:0")  # C 跳过
    assert [c["text"] for c in claim(rid, 4.0).json()["cues"]] == ["E"]

    # 回跳到 2:1:0（进度此刻 4.0）：本次不产生跳过，也不恢复 C、不重发 B/E
    data = locate(rid, 4.0, "2:1:0").json()
    assert data["skipped"] == []
    assert data["origin"]["position"] == "2:1:0"
    statuses = {c["text"]: c["status"] for c in data["cues"]}
    assert statuses == {"B": "delivered", "C": "skipped", "E": "delivered"}

    # 再领到任意秒数都不会冒出 C 或重发
    assert claim(rid, 1000.0).json()["cues"] == []


def test_locate_requires_elapsed_equal_to_claim_progress():
    score = create_score()
    r = create_rehearsal(
        score["id"], [{"position": "3:1:0", "text": "C"}]
    )
    rid = r["id"]
    claim(rid, 2.0)
    resp = locate(rid, 3.0, "4:1:0")  # 进度是 2.0，提交 3.0
    assert resp.status_code == 409
    detail = resp.json()["detail"][0]
    assert detail["elapsed_seconds"] == 3.0
    assert detail["last_elapsed_seconds"] == 2.0
    assert detail["loc"] == ["body", "elapsed_seconds"]

    # 状态不变：起点未更新、提示仍待发，随后按正确秒数仍可定位
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["origin"]["total_ticks"] == 0
    assert got["cues"][0]["status"] == "pending"
    ok = locate(rid, 2.0, "4:1:0")
    assert ok.status_code == 200


def test_locate_rejects_bad_position_and_nonfinite_elapsed():
    score = create_score()
    r = create_rehearsal(score["id"], [{"position": "2:1:0", "text": "B"}])
    rid = r["id"]

    bad_pos = locate(rid, 0.0, "1:9:0")  # 超出 4/4
    assert bad_pos.status_code == 422
    assert bad_pos.json()["detail"][0]["loc"] == ["body", "position"]

    assert locate(rid, -0.5, "1:1:0").status_code == 422
    for raw in ("NaN", "Infinity", "-Infinity"):
        resp = client.post(
            f"/rehearsals/{rid}/locate",
            content=f'{{"elapsed_seconds": {raw}, "position": "1:1:0"}}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422, raw

    # 全部拒绝：起点与进度不变
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["origin"]["total_ticks"] == 0
    assert got["last_elapsed_seconds"] == 0.0
    assert got["cues"][0]["status"] == "pending"


def test_locate_unknown_rehearsal_is_404():
    resp = locate("deadbeef", 0.0, "1:1:0")
    assert resp.status_code == 404
    assert resp.json()["detail"][0]["loc"] == ["path", "rehearsal_id"]


# ---------------------------------------------------------------------- #
# 定位 + 发布交错：发布只重算待发提示，不动起点/已发/跳过
# ---------------------------------------------------------------------- #
def test_publish_after_locate_retimes_pending_but_keeps_origin_and_skips():
    score = create_score()
    sid = score["id"]
    r = create_rehearsal(
        sid,
        [
            {"position": "2:1:0", "text": "B"},  # 已发 rev0 / 2.0s
            {"position": "3:1:0", "text": "C"},  # 定位跳过
            {"position": "5:1:0", "text": "E"},  # 待发
        ],
    )
    rid = r["id"]
    claim(rid, 2.0)
    locate(rid, 2.0, "4:1:0")  # 起点 (4:1:0=6.0s, 累计 2.0s)

    # 发布 60 BPM：全图秒数翻倍。起点位置变为 12.0s、E 变为 16.0s，
    # 间距仍是 4.0s -> E 触发秒数 2.0+4.0 = 6.0（相对秒数不变的特例）；
    # 为体现“按最新修订重算”，再定位一次制造非对齐间距：
    pub = client.post(
        f"/scores/{sid}/revisions/0/publish", json={"tempo_points": SLOW_POINTS}
    )
    assert pub.status_code == 200

    got = client.get(f"/rehearsals/{rid}").json()
    # 定位起点（tick + 提交秒数）不因发布而变
    assert got["origin"] == {
        "position": "4:1:0",
        "total_ticks": 5760,
        "elapsed_seconds": 2.0,
    }
    b, c, e = got["cues"]
    assert b["status"] == "delivered" and b["revision"] == 0
    assert math.isclose(b["trigger_seconds"], 2.0)
    assert c["status"] == "skipped" and c["skipped_revision"] == 0
    assert e["status"] == "pending"

    # E 按 rev1 重新定时：16.0 - 12.0 = 4.0 间距 -> 触发 6.0
    assert claim(rid, 5.999).json()["cues"] == []
    late = claim(rid, 6.0).json()
    assert [x["text"] for x in late["cues"]] == ["E"]
    assert late["revision"] == 1
    assert math.isclose(late["cues"][0]["trigger_seconds"], 6.0)


def test_publish_after_locate_can_change_relative_trigger_seconds():
    """非线性整比缩放时，发布后起点到提示的速度图时长会变。"""
    sid = create_score()["id"]
    # 提示：4:1:0（目标处）与 5:1:0；120 BPM 下一小节 2.0s
    r = create_rehearsal(
        sid,
        [
            {"position": "4:1:0", "text": "T"},
            {"position": "5:1:0", "text": "E"},
        ],
    )
    rid = r["id"]
    locate(rid, 0.0, "4:1:0")  # 起点 (4:1:0, 0.0s)

    # 自定义替换图：1:1:0 起 240 BPM（一小节 1.0s）。锁定锚点只有 1:1:0，
    # 偏移 0，允许发布。
    pub = client.post(
        f"/scores/{sid}/revisions/0/publish", json={"tempo_points": FAST_POINTS}
    )
    assert pub.status_code == 200
    # T 位于目标 tick -> 触发恒为 0.0（同秒可领）；E 在新图距起点 1.0s
    # （旧图为 2.0s）-> 触发 1.0，体现“按最新修订重算”。
    assert [x["text"] for x in claim(rid, 0.0).json()["cues"]] == ["T"]
    assert claim(rid, 0.999).json()["cues"] == []
    late = claim(rid, 1.0).json()
    assert [x["text"] for x in late["cues"]] == ["E"]
    assert math.isclose(late["cues"][0]["trigger_seconds"], 1.0)
    assert late["cues"][0]["revision"] == 1


# ---------------------------------------------------------------------- #
# 定位/领取/发布交错：同一提示不能既跳过又发放
# ---------------------------------------------------------------------- #
def test_concurrent_locate_and_claim_never_skip_and_deliver_same_cue(fresh_db):
    """多连接并发：一半在 4:1:0 定位（跳过之前的提示），一半领取到点。

    定位与领取共用写锁串行化，每条提示最终状态只能是
    delivered / skipped / pending 之一；跳过集合与发放集合不相交。
    """
    from app.db import OriginMismatch, Store
    from app.main import _due_cues, _skip_before_target

    score = create_score()
    r = create_rehearsal(
        score["id"],
        [{"position": f"{m}:1:0", "text": f"cue{m}"} for m in range(2, 8)],
    )
    rid = r["id"]
    db_path = fresh_db

    n_workers = 8
    stores = [Store(str(db_path)) for _ in range(n_workers)]
    barrier = threading.Barrier(n_workers)
    errors: list[Exception] = []
    rejected: list[str] = []

    def worker(i, store):
        try:
            barrier.wait(timeout=10)
            if i % 2 == 0:
                try:
                    store.locate(rid, 0.0, 5760, _skip_before_target)  # 跳 4:1:0
                except OriginMismatch:
                    # 领取先到时进度变为 1000，定位秒数 0 必须被拒绝且状态不变
                    rejected.append("locate")
            else:
                store.claim_cues(
                    rid, 1000.0, lambda s, p: _due_cues(s, p, 1000.0)
                )
        except Exception as exc:  # pragma: no cover - 失败时下面断言暴露
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i, s))
        for i, s in enumerate(stores)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    # 若领取先提交，后续定位都应被 409 拒绝（秒数 0 != 进度 1000），
    # 全部提示发放、没有跳过；若某个定位先到，则前两个提示跳过、其余发放。
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    delivered = {
        row[0]
        for row in conn.execute(
            "SELECT cue_index FROM cue_deliveries WHERE rehearsal_id = ?", (rid,)
        )
    }
    skipped = {
        row[0]
        for row in conn.execute(
            "SELECT cue_index FROM cue_skips WHERE rehearsal_id = ?", (rid,)
        )
    }
    origin = conn.execute(
        "SELECT origin_ticks, origin_elapsed FROM rehearsals WHERE id = ?", (rid,)
    ).fetchone()
    conn.close()
    assert not (delivered & skipped)
    if skipped:
        # 定位先到：目标之前的提示（2:1:0、3:1:0）跳过，其余发放，起点已更新
        assert skipped == {0, 1}
        assert origin == (5760, 0.0)
    else:
        # 领取先到：所有定位都被 409 拒绝，起点保持 (0, 0.0)
        assert rejected
        assert origin == (0, 0.0)
    assert delivered | skipped == set(range(6))


# ---------------------------------------------------------------------- #
# 持久化：定位起点与跳过状态重启后保留，可继续领取
# ---------------------------------------------------------------------- #
def test_locate_state_survives_store_restart(fresh_db, monkeypatch):
    db_path = fresh_db
    score = create_score()
    r = create_rehearsal(
        score["id"],
        [
            {"position": "2:1:0", "text": "B"},
            {"position": "3:1:0", "text": "C"},
            {"position": "5:1:0", "text": "E"},
        ],
    )
    rid = r["id"]
    claim(rid, 2.0)
    locate(rid, 2.0, "4:1:0")  # C 跳过，起点 (4:1:0, 2.0)

    import app.main as main
    from app.db import Store

    monkeypatch.setattr(main, "store", Store(str(db_path)))

    got = client.get(f"/rehearsals/{rid}").json()
    assert got["origin"]["position"] == "4:1:0"
    assert got["origin"]["elapsed_seconds"] == 2.0
    assert [c["status"] for c in got["cues"]] == [
        "delivered",
        "skipped",
        "pending",
    ]
    # C 不会因重启恢复；E 仍按定位后的相对秒数（2.0 + 2.0 = 4.0）领取
    late = claim(rid, 4.0).json()
    assert [c["text"] for c in late["cues"]] == ["E"]
    assert math.isclose(late["cues"][0]["trigger_seconds"], 4.0)

    after = client.get(f"/rehearsals/{rid}").json()
    assert {c["text"]: c["status"] for c in after["cues"]} == {
        "B": "delivered",
        "C": "skipped",
        "E": "delivered",
    }



# ---------------------------------------------------------------------- #
# 旧版库迁移：无定位起点列/无 cue_skips 表的库打开后自动升级，
# 旧排练仍按绝对秒数计时
# ---------------------------------------------------------------------- #
def test_old_schema_db_is_migrated_and_keeps_absolute_timing(tmp_path):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE scores (id TEXT PRIMARY KEY, ppq INTEGER NOT NULL,
            time_signatures TEXT NOT NULL, tempo_points TEXT NOT NULL,
            revision INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE anchors (score_id TEXT NOT NULL, position_index INTEGER NOT NULL,
            position TEXT NOT NULL, locked INTEGER NOT NULL, total_ticks INTEGER NOT NULL,
            original_seconds REAL NOT NULL, current_seconds REAL NOT NULL,
            tick_seconds REAL NOT NULL, PRIMARY KEY (score_id, position_index));
        CREATE TABLE rehearsals (id TEXT PRIMARY KEY, score_id TEXT NOT NULL,
            last_elapsed REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE rehearsal_cues (rehearsal_id TEXT NOT NULL, cue_index INTEGER NOT NULL,
            position TEXT NOT NULL, text TEXT NOT NULL, total_ticks INTEGER NOT NULL,
            PRIMARY KEY (rehearsal_id, cue_index));
        CREATE TABLE cue_deliveries (rehearsal_id TEXT NOT NULL, cue_index INTEGER NOT NULL,
            trigger_seconds REAL NOT NULL, revision INTEGER NOT NULL,
            elapsed_seconds REAL NOT NULL,
            delivered_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (rehearsal_id, cue_index));
        """
    )
    points = json.dumps(
        [{"position": "1:1:0", "bpm": 120, "curve": "step"}], ensure_ascii=False
    )
    conn.execute(
        "INSERT INTO scores VALUES ('s1', 480, '[]', ?, 0, datetime('now'))",
        (points,),
    )
    conn.execute(
        "INSERT INTO anchors VALUES ('s1', 0, '1:1:0', 1, 0, 0.0, 0.0, 0.001)"
    )
    conn.execute(
        "INSERT INTO rehearsals (id, score_id, last_elapsed) VALUES ('r1','s1', 2.0)"
    )
    conn.execute("INSERT INTO rehearsal_cues VALUES ('r1', 0, '2:1:0', 'B', 1920)")
    conn.execute("INSERT INTO rehearsal_cues VALUES ('r1', 1, '3:1:0', 'C', 3840)")
    conn.execute(
        "INSERT INTO cue_deliveries VALUES ('r1', 0, 2.0, 0, 2.0, datetime('now'))"
    )
    conn.commit()
    conn.close()

    from app.db import Store
    from app.main import _due_cues

    # 打开旧库：自动补 origin_ticks / origin_elapsed 列与 cue_skips 表
    store = Store(str(db_path))
    got = store.get_rehearsal("r1")
    assert got["origin_ticks"] == 0
    assert got["origin_elapsed_seconds"] == 0.0
    assert [c["status"] for c in got["cues"]] == ["delivered", "pending"]

    # 旧排练未定位：C 仍按全图绝对秒数 4.0 触发
    out = store.claim_cues("r1", 4.0, lambda s, p: _due_cues(s, p, 4.0))
    assert [(c["index"], c["trigger_seconds"]) for c in out["cues"]] == [(1, 4.0)]
