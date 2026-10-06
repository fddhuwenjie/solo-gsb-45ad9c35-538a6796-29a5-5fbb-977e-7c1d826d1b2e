"""FastAPI /convert 端到端测试。"""

import math

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

BASE_BODY = {
    "ppq": 480,
    "time_signatures": [
        {"measure": 1, "numerator": 4, "denominator": 4},
        {"measure": 5, "numerator": 3, "denominator": 4},
    ],
    "tempo_points": [
        {"position": "1:1:0", "bpm": 120, "curve": "step"},
        {"position": "2:1:0", "bpm": 60, "curve": "linear"},
        {"position": "3:1:0", "bpm": 180, "curve": "step"},
        {"position": "5:1:0", "bpm": 90, "curve": "step"},
    ],
}


def test_health_and_root():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").json()["convert"] == "/convert"


def test_convert_position_to_seconds():
    resp = client.post("/convert", json={**BASE_BODY, "position": "1:2:0"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["position"] == "1:2:0"
    assert data["total_ticks"] == 480
    assert math.isclose(data["seconds"], 0.5)
    assert data["roundtrip"]["source"] == "position"
    assert data["roundtrip"]["within_one_tick"] is True
    assert data["roundtrip"]["residual_ticks"] <= 1.0


def test_convert_seconds_to_position_and_back():
    r1 = client.post("/convert", json={**BASE_BODY, "position": "6:1:0"})
    assert r1.status_code == 200, r1.text
    seconds = r1.json()["seconds"]

    r2 = client.post("/convert", json={**BASE_BODY, "seconds": seconds})
    assert r2.status_code == 200, r2.text
    d2 = r2.json()
    assert d2["position"] == "6:1:0"
    assert d2["total_ticks"] == r1.json()["total_ticks"]
    assert d2["roundtrip"]["within_one_tick"] is True
    assert d2["roundtrip"]["residual_ticks"] <= 1.0
    assert math.isclose(d2["roundtrip"]["residual_seconds"], 0.0, abs_tol=1e-9)


def test_convert_origin_and_tail_hold():
    resp = client.post(
        "/convert",
        json={
            "ppq": 480,
            "time_signatures": [],
            "tempo_points": [{"position": "1:1:0", "bpm": 100, "curve": "step"}],
            "position": "10:1:0",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 9 个 4/4 小节 * 4 拍；100 BPM 一拍 0.6 秒
    assert data["total_ticks"] == 9 * 4 * 480
    assert math.isclose(data["seconds"], 9 * 4 * 0.6)


def test_linear_ramp_example_seconds_input():
    resp = client.post(
        "/convert",
        json={
            "ppq": 480,
            "time_signatures": [],
            "tempo_points": [
                {"position": "1:1:0", "bpm": 60, "curve": "linear"},
                {"position": "1:2:0", "bpm": 120, "curve": "step"},
            ],
            "seconds": math.log(2),
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["position"] == "1:2:0"
    assert data["total_ticks"] == 480


def test_convert_compound_meter_6_8_beat_semantics():
    # PPQ=480、6/8、120 BPM：1:4:0 = 第 4 个八分音符拍 = 720 tick = 0.75 秒
    resp = client.post(
        "/convert",
        json={
            "ppq": 480,
            "time_signatures": [{"measure": 1, "numerator": 6, "denominator": 8}],
            "tempo_points": [{"position": "1:1:0", "bpm": 120, "curve": "step"}],
            "position": "1:4:0",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["position"] == "1:4:0"
    assert data["total_ticks"] == 720
    assert math.isclose(data["seconds"], 0.75)
    assert data["roundtrip"]["within_one_tick"] is True
    assert data["roundtrip"]["position_roundtrip"] == "1:4:0"

    # 反向：0.75 秒 -> 1:4:0
    r2 = client.post(
        "/convert",
        json={
            "ppq": 480,
            "time_signatures": [{"measure": 1, "numerator": 6, "denominator": 8}],
            "tempo_points": [{"position": "1:1:0", "bpm": 120, "curve": "step"}],
            "seconds": 0.75,
        },
    )
    assert r2.status_code == 200, r2.text
    d2 = r2.json()
    assert d2["position"] == "1:4:0"
    assert d2["total_ticks"] == 720


def test_error_compound_meter_beat_tick_out_of_range():
    body = {
        "ppq": 480,
        "time_signatures": [{"measure": 1, "numerator": 6, "denominator": 8}],
        "tempo_points": [{"position": "1:1:0", "bpm": 120, "curve": "step"}],
    }
    # 合法边界：第 6 拍最后一 tick
    ok = client.post("/convert", json={**body, "position": "1:6:239"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["total_ticks"] == 6 * 240 - 1
    # 超过第 6 拍 -> 422
    r1 = client.post("/convert", json={**body, "position": "1:7:0"})
    assert r1.status_code == 422
    assert any("拍必须在 1..6 内" in e["msg"] for e in r1.json()["detail"])
    # tick 超出单拍范围（6/8、PPQ=480 下每拍 240 tick）-> 422
    r2 = client.post("/convert", json={**body, "position": "1:1:240"})
    assert r2.status_code == 422
    assert any("[0, 240)" in e["msg"] for e in r2.json()["detail"])


def test_error_meter_beat_not_integer_ticks():
    # PPQ=480 下 1/256：单拍 4*480/256 = 7.5 tick，非整数 -> 422
    resp = client.post(
        "/convert",
        json={
            "ppq": 480,
            "time_signatures": [{"measure": 1, "numerator": 1, "denominator": 256}],
            "tempo_points": [{"position": "1:1:0", "bpm": 120, "curve": "step"}],
            "position": "1:1:0",
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any(e.get("index") == 0 for e in detail)
    assert any("单拍长度不是整数 tick" in e["msg"] for e in detail)


def test_error_first_point_not_origin():
    resp = client.post(
        "/convert",
        json={
            "ppq": 480,
            "time_signatures": [],
            "tempo_points": [{"position": "1:2:0", "bpm": 120, "curve": "step"}],
            "position": "1:2:0",
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and detail
    assert any(e.get("index") == 0 for e in detail)
    assert any("1:1:0" in e["msg"] for e in detail)


def test_error_non_increasing_points_indexed():
    resp = client.post(
        "/convert",
        json={
            "ppq": 480,
            "time_signatures": [],
            "tempo_points": [
                {"position": "1:1:0", "bpm": 120, "curve": "step"},
                {"position": "3:1:0", "bpm": 100, "curve": "step"},
                {"position": "2:1:0", "bpm": 100, "curve": "step"},
            ],
            "seconds": 1.0,
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any(e.get("index") == 2 for e in detail)
    assert all({"loc", "msg", "index"} <= set(e) for e in detail)


def test_error_non_positive_bpm_indexed():
    resp = client.post(
        "/convert",
        json={
            "ppq": 480,
            "time_signatures": [],
            "tempo_points": [
                {"position": "1:1:0", "bpm": 120, "curve": "step"},
                {"position": "2:1:0", "bpm": -3, "curve": "step"},
            ],
            "position": "2:1:0",
        },
    )
    assert resp.status_code == 422
    assert any(e.get("index") == 1 for e in resp.json()["detail"])


def test_error_bad_meter_indexed():
    resp = client.post(
        "/convert",
        json={
            "ppq": 480,
            "time_signatures": [
                {"measure": 1, "numerator": 4, "denominator": 4},
                {"measure": 2, "numerator": 5, "denominator": 6},
            ],
            "tempo_points": [{"position": "1:1:0", "bpm": 120, "curve": "step"}],
            "position": "1:1:0",
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any(e.get("index") == 1 for e in detail)


def test_error_missing_both_inputs():
    resp = client.post("/convert", json=BASE_BODY)
    assert resp.status_code == 422
    assert any("position" in e["msg"] and "seconds" in e["msg"]
               for e in resp.json()["detail"])


def test_error_ppq_invalid():
    resp = client.post(
        "/convert",
        json={**BASE_BODY, "ppq": 0, "position": "1:1:0"},
    )
    assert resp.status_code == 422


def test_error_negative_seconds_and_empty_points():
    r1 = client.post(
        "/convert",
        json={
            "ppq": 480,
            "time_signatures": [],
            "tempo_points": [{"position": "1:1:0", "bpm": 120, "curve": "step"}],
            "seconds": -0.5,
        },
    )
    assert r1.status_code == 422
    assert any("非负" in e["msg"] for e in r1.json()["detail"])

    r2 = client.post(
        "/convert",
        json={"ppq": 480, "time_signatures": [], "tempo_points": [], "seconds": 1.0},
    )
    assert r2.status_code == 422


def test_error_aggregates_meter_and_tempo_indexes():
    # 一次请求里同时存在：拍号分母非法(0)、首点不在原点(0)、BPM 非正(1)、位置重复(2)
    resp = client.post(
        "/convert",
        json={
            "ppq": 480,
            "time_signatures": [{"measure": 2, "numerator": 5, "denominator": 6}],
            "tempo_points": [
                {"position": "1:2:0", "bpm": 120, "curve": "step"},
                {"position": "2:1:0", "bpm": 0, "curve": "linear"},
                {"position": "2:1:0", "bpm": 80, "curve": "step"},
            ],
            "position": "2:1:0",
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    indexes = {e["index"] for e in detail}
    assert {0, 1, 2} <= indexes


def test_error_tick_out_of_range_indexed():
    resp = client.post(
        "/convert",
        json={
            "ppq": 480,
            "time_signatures": [],
            "tempo_points": [
                {"position": "1:1:0", "bpm": 120, "curve": "step"},
                {"position": "1:1:999", "bpm": 120, "curve": "step"},
            ],
            "position": "1:1:0",
        },
    )
    assert resp.status_code == 422
    assert any(e.get("index") == 1 for e in resp.json()["detail"])
