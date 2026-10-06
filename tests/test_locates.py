"""排练定位（跳转）：跳过/重定时、回跳、发布交错、互斥与持久化测试。"""

import math
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """每个测试用独立 SQLite，并重新初始化模块级 store。"""
    db_path = tmp_path / "locates.db"
    monkeypatch.setenv("TEMPO_DB_PATH", str(db_path))

    import app.main as main
    from app.db import Store

    monkeypatch.setattr(main, "store", Store(str(db_path)))
    yield db_path


from app.main import app  # noqa: E402

client = TestClient(app)

# 120 BPM、4/4、PPQ=480：一拍 0.5s、一小节 2.0s
SCORE_BODY = {
    "ppq": 480,
    "time_signatures": [],
    "tempo_points": [{"position": "1:1:0", "bpm": 120, "curve": "step"}],
    "anchors": [{"position": "1:1:0", "locked": True}],
}
SLOW_POINTS = [{"position": "1:1:0", "bpm": 60, "curve": "step"}]


def create_score(points=None):
    body = dict(SCORE_BODY)
    if points is not None:
        body["tempo_points"] = points
    resp = client.post("/scores", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def create_rehearsal(score_id, cues):
    resp = client.post("/rehearsals", json={"score_id": score_id, "cues": cues})
    assert resp.status_code == 201, resp.text
    return resp.json()


def claim(rid, elapsed):
    return client.post(
        f"/rehearsals/{rid}/claims", json={"elapsed_seconds": elapsed}
    )


def locate(rid, elapsed, position):
    return client.post(
        f"/rehearsals/{rid}/locates",
        json={"elapsed_seconds": elapsed, "position": position},
    )


def statuses(rid):
    return {c["index"]: c["status"] for c in client.get(f"/rehearsals/{rid}").json()["cues"]}


# ---------------------------------------------------------------------- #
# 未定位排练保持原有计时
# ---------------------------------------------------------------------- #
def test_unlocated_rehearsal_keeps_original_timing_origin():
    score = create_score()
    r = create_rehearsal(score["id"], [{"position": "2:1:0", "text": "B"}])
    assert r["located"] is False
    assert r["origin_elapsed_seconds"] == 0.0
    assert r["origin_position"] == "1:1:0"
    assert r["origin_ticks"] == 0
    got = client.get(f"/rehearsals/{r['id']}").json()
    assert got["cues"][0]["status"] == "pending"
    assert got["cues"][0]["delivered"] is False
    assert got["cues"][0]["skipped"] is False
    data = claim(r["id"], 2.0).json()
    assert math.isclose(data["cues"][0]["trigger_seconds"], 2.0)


# ---------------------------------------------------------------------- #
# 前跳：之前的未发提示记跳过；目标处提示可在同一秒领取；
# 之后的提示按“目标 -> 提示”时长定时
# ---------------------------------------------------------------------- #
def test_locate_forward_marks_earlier_pending_skipped_and_retimes():
    score = create_score()
    r = create_rehearsal(
        score["id"],
        [
            {"position": "1:2:0", "text": "A"},  # 0.5s
            {"position": "2:1:0", "text": "B"},  # 2.0s
            {"position": "3:1:0", "text": "C"},  # 4.0s（目标）
            {"position": "4:1:0", "text": "D"},  # 6.0s
        ],
    )
    rid = r["id"]
    assert claim(rid, 0.5).json()["cues"][0]["text"] == "A"  # 进度到 0.5

    resp = locate(rid, 0.5, "3:1:0")
    assert resp.status_code == 200, resp.text
    got = resp.json()
    # 计时起点更新为（0.5s, 3:1:0, 3840 tick），领取进度不变
    assert got["located"] is True
    assert got["origin_elapsed_seconds"] == 0.5
    assert got["origin_position"] == "3:1:0"
    assert got["origin_ticks"] == 3840
    assert got["last_elapsed_seconds"] == 0.5
    # A 已发不动；B 未发且在目标之前 -> 跳过；C/D 待发
    assert [c["status"] for c in got["cues"]] == [
        "delivered",
        "skipped",
        "pending",
        "pending",
    ]
    b = got["cues"][1]
    assert b["delivered"] is False and b["skipped"] is True
    assert b["trigger_seconds"] is None and b["revision"] is None

    # 目标处的 C 可在定位提交的同一秒（0.5s）领取，触发秒数即起点秒数
    same = claim(rid, 0.5)
    assert same.status_code == 200
    cues = same.json()["cues"]
    assert [c["text"] for c in cues] == ["C"]
    assert math.isclose(cues[0]["trigger_seconds"], 0.5)
    assert cues[0]["revision"] == 0

    # D：0.5 + (5760-3840) tick / (480 tick/拍 * 2拍/秒^-1...) = 0.5+2.0 = 2.5s
    before = claim(rid, 2.4)
    assert before.json()["cues"] == []
    d = claim(rid, 2.5).json()["cues"]
    assert [c["text"] for c in d] == ["D"]
    assert math.isclose(d[0]["trigger_seconds"], 2.5)

    # 查询可区分三种状态
    final = client.get(f"/rehearsals/{rid}").json()
    assert [c["status"] for c in final["cues"]] == [
        "delivered",
        "skipped",
        "delivered",
        "delivered",
    ]
    # 被跳过的 B 永远不发放，即使进度远超其原始触发秒数
    assert final["cues"][1]["trigger_seconds"] is None


def test_locate_at_zero_without_any_claim_skips_earlier_cues():
    score = create_score()
    r = create_rehearsal(
        score["id"],
        [
            {"position": "2:1:0", "text": "B"},
            {"position": "3:1:0", "text": "C"},
        ],
    )
    rid = r["id"]
    resp = locate(rid, 0.0, "3:1:0")
    assert resp.status_code == 200, resp.text
    assert statuses(rid) == {0: "skipped", 1: "pending"}
    # 目标处 C 在 0 秒即可领
    cues = claim(rid, 0.0).json()["cues"]
    assert [c["text"] for c in cues] == ["C"]
    assert math.isclose(cues[0]["trigger_seconds"], 0.0)


def test_cue_exactly_at_target_is_not_skipped():
    score = create_score()
    r = create_rehearsal(
        score["id"],
        [
            {"position": "2:1:0", "text": "B"},  # tick 1920，目标本身
            {"position": "3:1:0", "text": "C"},
        ],
    )
    rid = r["id"]
    resp = locate(rid, 0.0, "2:1:0")
    assert resp.status_code == 200
    assert statuses(rid) == {0: "pending", 1: "pending"}
    cues = claim(rid, 0.0).json()["cues"]
    assert [c["text"] for c in cues] == ["B"]


# ---------------------------------------------------------------------- #
# 定位秒数不等于当前领取进度：409 且状态不变
# ---------------------------------------------------------------------- #
def test_locate_with_elapsed_not_equal_to_progress_is_409_unchanged():
    score = create_score()
    r = create_rehearsal(
        score["id"],
        [
            {"position": "2:1:0", "text": "B"},
            {"position": "3:1:0", "text": "C"},
        ],
    )
    rid = r["id"]
    claim(rid, 2.0)  # B 已发，进度 2.0

    resp = locate(rid, 1.0, "4:1:0")
    assert resp.status_code == 409
    detail = resp.json()["detail"][0]
    assert detail["elapsed_seconds"] == 1.0
    assert detail["last_elapsed_seconds"] == 2.0

    # 状态不变：没有跳过、起点未更新、进度仍是 2.0
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["located"] is False
    assert got["origin_position"] == "1:1:0"
    assert got["last_elapsed_seconds"] == 2.0
    assert [c["status"] for c in got["cues"]] == ["delivered", "pending"]

    # 用正确秒数仍可定位
    ok = locate(rid, 2.0, "4:1:0")
    assert ok.status_code == 200
    assert ok.json()["origin_elapsed_seconds"] == 2.0


def test_locate_rejects_negative_or_nonfinite_elapsed():
    score = create_score()
    r = create_rehearsal(score["id"], [{"position": "2:1:0", "text": "B"}])
    rid = r["id"]
    assert locate(rid, -1.0, "2:1:0").status_code == 422
    for raw in ("NaN", "Infinity", "-Infinity"):
        resp = client.post(
            f"/rehearsals/{rid}/locates",
            content=f'{{"elapsed_seconds": {raw}, "position": "2:1:0"}}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422, raw
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["located"] is False and got["cues"][0]["status"] == "pending"


def test_locate_bad_position_is_422_and_state_unchanged():
    score = create_score()
    r = create_rehearsal(
        score["id"], [{"position": "2:1:0", "text": "B"}]
    )
    rid = r["id"]
    resp = locate(rid, 0.0, "1:9:0")  # 拍超出 4/4
    assert resp.status_code == 422
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["located"] is False
    assert got["cues"][0]["status"] == "pending"
    # 位置格式非法同样 422
    assert locate(rid, 0.0, "nope").status_code == 422


def test_locate_unknown_rehearsal_is_404():
    resp = locate("deadbeef", 0.0, "1:1:0")
    assert resp.status_code == 404
    assert resp.json()["detail"][0]["loc"] == ["path", "rehearsal_id"]


# ---------------------------------------------------------------------- #
# 回跳：不重发已发提示，不恢复已跳过提示；起点仍按新目标更新
# ---------------------------------------------------------------------- #
def test_backward_locate_neither_redelivers_nor_restores():
    score = create_score()
    r = create_rehearsal(
        score["id"],
        [
            {"position": "2:1:0", "text": "B"},  # 先被跳过
            {"position": "3:1:0", "text": "C"},  # 先发放
            {"position": "4:1:0", "text": "D"},
        ],
    )
    rid = r["id"]
    locate(rid, 0.0, "3:1:0")  # B 跳过；C 在目标
    claim(rid, 0.0)  # 发放 C，进度 0.0
    assert statuses(rid) == {0: "skipped", 1: "delivered", 2: "pending"}

    # 回跳到 2:1:0（已跳过的 B 恰在目标、已发的 C 在目标之后）
    resp = locate(rid, 0.0, "2:1:0")
    assert resp.status_code == 200
    got = resp.json()
    assert got["origin_position"] == "2:1:0"
    assert got["origin_ticks"] == 1920
    # B 仍跳过（不恢复）、C 仍已发（不重发）、D 仍待发（按新起点重定时）
    assert [c["status"] for c in got["cues"]] == [
        "skipped",
        "delivered",
        "pending",
    ]
    # 同一秒领取：没有任何新提示（B 不恢复、C 不重发）
    assert claim(rid, 0.0).json()["cues"] == []
    # D 现按 2:1:0 -> 4:1:0 的时长定时：0.0 + 4.0 = 4.0s
    assert claim(rid, 3.9).json()["cues"] == []
    d = claim(rid, 4.0).json()["cues"]
    assert [c["text"] for c in d] == ["D"]
    assert math.isclose(d[0]["trigger_seconds"], 4.0)


# ---------------------------------------------------------------------- #
# 发布与定位交错：待发提示按最新已提交修订重新计时；
# 已发记录、跳过状态与定位起点均不变
# ---------------------------------------------------------------------- #
def test_publish_after_locate_retimes_pending_but_keeps_origin_and_skips():
    score = create_score()
    sid = score["id"]
    r = create_rehearsal(
        sid,
        [
            {"position": "2:1:0", "text": "B"},  # 定位时跳过
            {"position": "3:1:0", "text": "C"},  # 定位目标
            {"position": "4:1:0", "text": "D"},
        ],
    )
    rid = r["id"]
    locate(rid, 0.0, "3:1:0")
    assert statuses(rid) == {0: "skipped", 1: "pending", 2: "pending"}

    # 发布 60 BPM（所有“目标 -> 提示”时长翻倍）
    pub = client.post(
        f"/scores/{sid}/revisions/0/publish", json={"tempo_points": SLOW_POINTS}
    )
    assert pub.status_code == 200

    got = client.get(f"/rehearsals/{rid}").json()
    # 定位起点不变
    assert got["located"] is True
    assert got["origin_position"] == "3:1:0"
    assert got["origin_ticks"] == 3840
    assert got["origin_elapsed_seconds"] == 0.0
    # 跳过状态不变
    assert got["cues"][0]["status"] == "skipped"

    # C（目标处）仍在 0s 到点；发放记录用新修订号 1
    c = claim(rid, 0.0).json()["cues"]
    assert [x["text"] for x in c] == ["C"]
    assert math.isclose(c[0]["trigger_seconds"], 0.0)
    assert c[0]["revision"] == 1

    # D：0 + (4:1:0 12s - 3:1:0 8s) = 4.0s（60 BPM）；3.9 未到
    assert claim(rid, 3.9).json()["cues"] == []
    d = claim(rid, 4.0).json()["cues"]
    assert [x["text"] for x in d] == ["D"]
    assert math.isclose(d[0]["trigger_seconds"], 4.0)
    assert d[0]["revision"] == 1

    # B 自始至终跳过，不随发布恢复或发放
    final = client.get(f"/rehearsals/{rid}").json()
    assert [x["status"] for x in final["cues"]] == [
        "skipped",
        "delivered",
        "delivered",
    ]


def test_publish_between_locate_and_claim_uses_committed_revision():
    """定位后、首次领取前发布：触发秒数按新修订，记录修订与之一致。"""
    score = create_score()
    sid = score["id"]
    r = create_rehearsal(
        sid,
        [
            {"position": "2:1:0", "text": "B"},
            {"position": "3:1:0", "text": "C"},
        ],
    )
    rid = r["id"]
    # 先领取 B（rev0），进度 2.0
    assert claim(rid, 2.0).json()["cues"][0]["revision"] == 0
    # 定位到 3:1:0（C 在目标）
    locate(rid, 2.0, "3:1:0")
    # 发布 60 BPM：定位起点（2.0s, 3:1:0）不变；C 在目标处仍是 2.0s
    client.post(
        f"/scores/{sid}/revisions/0/publish", json={"tempo_points": SLOW_POINTS}
    )
    c = claim(rid, 2.0).json()["cues"]
    assert [x["text"] for x in c] == ["C"]
    assert math.isclose(c[0]["trigger_seconds"], 2.0)
    assert c[0]["revision"] == 1  # 发放记录修订 = 计算所用修订


# ---------------------------------------------------------------------- #
# 定位 / 领取交错：同一提示不能既跳过又发放
# ---------------------------------------------------------------------- #
def test_concurrent_locate_and_claim_never_skips_and_delivers_same_cue(fresh_db):
    """多连接并发：前跳定位与足额领取交错，同批提示状态互斥且完整。

    定位要求提交秒数恰为当前进度：若定位在第一次领取之前执行（进度
    仍为 0），按约定它必然被 409 拒绝且状态不变——这也是合法的交错
    结果，唯一不允许的是“同一提示既跳过又发放”。
    """
    from app.db import LocateMismatch, Store
    from app.main import _due_cues
    from app.tempo import build_tempo_map

    score = create_score()
    r = create_rehearsal(
        score["id"],
        [{"position": f"{m}:1:0", "text": f"cue{m}"} for m in range(1, 9)],
    )
    rid = r["id"]
    db_path = fresh_db

    n_workers = 6
    stores = [Store(str(db_path)) for _ in range(n_workers)]
    barrier = threading.Barrier(n_workers)
    errors: list[Exception] = []

    def resolve(score_, position):
        tm = build_tempo_map(
            score_["ppq"], score_["time_signatures"], score_["tempo_points"]
        )
        t = tm.mbt_to_ticks(position)
        return t, str(tm.ticks_to_mbt(t))

    def worker(i, store):
        try:
            barrier.wait(timeout=10)
            if i % 2 == 0:
                # 定位到 5:1:0（须以 8.0s 进度提交），前 4 个提示应跳过。
                # 若它在首次足额领取前抢入（进度仍 0），按约定被拒绝。
                try:
                    store.locate_rehearsal(rid, 8.0, "5:1:0", resolve)
                except LocateMismatch:
                    pass
            else:
                # 同一刻足额领取：到点提示发放
                store.claim_cues(
                    rid, 8.0, lambda s, p, o: _due_cues(s, p, 8.0, o)
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
    # 至少一个足额领取必然成功（3 个领取线程，不存在倒退冲突）
    got = client.get(f"/rehearsals/{rid}").json()
    assert got["last_elapsed_seconds"] == 8.0
    by_status = {"pending": [], "delivered": [], "skipped": []}
    for c in got["cues"]:
        by_status[c["status"]].append(c["index"])
    # 三条状态互斥且穷尽全部 8 条提示（同一条绝不可能既跳过又发放）
    assert sorted(
        by_status["pending"] + by_status["delivered"] + by_status["skipped"]
    ) == list(range(8))
    # 定位只可能跳过严格位于 5:1:0 之前的提示（前 4 个）
    assert set(by_status["skipped"]) <= {0, 1, 2, 3}
    # 被跳过的绝不可能同时已发放（集合由三态分组天然互斥，再显式确认）
    assert not (set(by_status["skipped"]) & set(by_status["delivered"]))


# ---------------------------------------------------------------------- #
# 持久化：重启后定位起点、跳过状态可读取并继续领取
# ---------------------------------------------------------------------- #
def test_locate_origin_and_skips_survive_store_restart(fresh_db, monkeypatch):
    import app.main as main
    from app.db import Store

    db_path = fresh_db
    score = create_score()
    r = create_rehearsal(
        score["id"],
        [
            {"position": "2:1:0", "text": "B"},
            {"position": "3:1:0", "text": "C"},
            {"position": "4:1:0", "text": "D"},
        ],
    )
    rid = r["id"]
    claim(rid, 0.5)  # 进度 0.5（无提示发放）
    locate(rid, 0.5, "3:1:0")  # B 跳过，起点 0.5s / 3:1:0
    assert statuses(rid) == {0: "skipped", 1: "pending", 2: "pending"}

    # 重新打开同一文件（不删除卷的 Compose 重启等价场景）
    monkeypatch.setattr(main, "store", Store(str(db_path)))

    got = client.get(f"/rehearsals/{rid}").json()
    assert got["located"] is True
    assert got["origin_position"] == "3:1:0"
    assert got["origin_ticks"] == 3840
    assert got["origin_elapsed_seconds"] == 0.5
    assert [c["status"] for c in got["cues"]] == [
        "skipped",
        "pending",
        "pending",
    ]

    # 定位秒数校验仍生效：不等于 0.5 -> 409
    assert locate(rid, 0.4, "4:1:0").status_code == 409

    # C 在起点同秒可领，D 于 2.5s 可领
    assert [c["text"] for c in claim(rid, 0.5).json()["cues"]] == ["C"]
    assert [c["text"] for c in claim(rid, 2.5).json()["cues"]] == ["D"]
