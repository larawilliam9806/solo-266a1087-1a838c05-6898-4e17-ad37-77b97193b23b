from __future__ import annotations

import itertools
import random

import pytest
from fastapi.testclient import TestClient

from app import coverage
from app.main import create_app


# ---------------------------------------------------------------------------
# 纯算法测试
# ---------------------------------------------------------------------------


def brute_force(roster, frames):
    """枚举所有排法，作为 DP 的对照答案（只用于小样本测试）。"""
    first = frames[0].frame_no
    last = frames[-1].frame_no
    scanned = {f.frame_no: f for f in frames}
    span = last - first + 1

    results = []
    for assignment in itertools.product(range(len(roster)), repeat=span):
        ok = True
        for pos in range(span):
            page = assignment[pos]
            frame = scanned.get(first + pos)
            if frame is not None and frame.candidates:
                if roster[page] not in frame.candidates:
                    ok = False
                    break
            if pos:
                delta = page - assignment[pos - 1]
                if delta not in (0, 1):
                    ok = False
                    break
        if ok:
            results.append(assignment)

    pages = {}
    for i, plate in enumerate(roster):
        scanned_possible = set()
        missed_possible = set()
        guaranteed = bool(results)
        for assignment in results:
            covered = False
            for pos, page in enumerate(assignment):
                if page != i:
                    continue
                frame_no = first + pos
                if frame_no in scanned:
                    scanned_possible.add(frame_no)
                    covered = True
                else:
                    missed_possible.add(frame_no)
            if not covered:
                guaranteed = False
        pages[plate] = {
            "possible_scanned_frames": sorted(scanned_possible),
            "possible_missed_frames": sorted(missed_possible),
            "guaranteed": guaranteed,
        }
    return bool(results), pages


def make_frames(spec):
    return [
        coverage.Frame(frame_no=no, candidates=tuple(cands))
        for no, cands in sorted(spec.items())
    ]


def test_matches_brute_force_on_random_small_cases():
    rng = random.Random(20261122)
    for _ in range(120):
        roster = [str(i + 1) for i in range(rng.randint(2, 4))]
        span = rng.randint(1, 5)
        frames = []
        for pos in range(span):
            if rng.random() < 0.35:
                continue
            k = rng.randint(0, 2)
            cands = tuple(rng.sample(roster, k)) if k < len(roster) else tuple(roster)
            frames.append(coverage.Frame(frame_no=100 + pos, candidates=cands))
        if not frames:
            continue

        expected_ok, expected_pages = brute_force(roster, frames)
        try:
            result = coverage.compute_volume(roster, frames)
            actual_ok = True
        except coverage.ArrangementError:
            actual_ok = False

        assert actual_ok == expected_ok
        if not expected_ok:
            continue
        for plate in roster:
            got = result["pages"][plate]
            want = expected_pages[plate]
            assert got["possible_scanned_frames"] == want["possible_scanned_frames"], (
                roster, frames, plate
            )
            assert got["possible_missed_frames"] == want["possible_missed_frames"], (
                roster, frames, plate
            )
            assert got["guaranteed"] == want["guaranteed"], (roster, frames, plate)


def test_guaranteed_vs_pending_vs_missing():
    roster = ["1", "2", "3"]
    # 帧 1 认不出（版1或2），帧 2=版2。排法只有 (1,2) 和 (2,2)。
    frames = make_frames({1: (), 2: ("2",)})
    result = coverage.compute_volume(roster, frames)
    pages = result["pages"]

    assert pages["1"]["guaranteed"] is False
    assert pages["1"]["possible_scanned_frames"] == [1]
    assert pages["2"]["guaranteed"] is True
    assert pages["2"]["possible_scanned_frames"] == [1, 2]
    assert pages["3"]["possible_scanned_frames"] == []


def test_missing_page_points_to_missed_frames_in_span():
    roster = ["1", "2", "3", "4", "5"]
    # 帧 1=版1、帧 3=版3，帧 2 是首末已扫帧之间的跳号。
    frames = make_frames({1: ("1",), 3: ("3",)})
    result = coverage.compute_volume(roster, frames)
    pages = result["pages"]

    assert pages["1"]["guaranteed"] is True
    assert pages["2"]["possible_scanned_frames"] == []
    assert pages["2"]["possible_missed_frames"] == [2]
    assert pages["3"]["guaranteed"] is True
    # 漏扫帧 2 夹在版1和版3之间，够不到版4、5。
    assert pages["4"]["possible_missed_frames"] == []
    assert pages["5"]["possible_missed_frames"] == []


def test_infeasible_volume_raises():
    roster = ["1", "2", "3"]
    frames = make_frames({1: ("3",), 2: ("1",)})
    with pytest.raises(coverage.ArrangementError):
        coverage.compute_volume(roster, frames)


def test_span_limit():
    roster = [str(i + 1) for i in range(600)]
    frames = make_frames({1: (), 501: ()})
    with pytest.raises(coverage.ArrangementError) as info:
        coverage.compute_volume(roster, frames)
    assert info.value.code == "span_too_large"

    frames = make_frames({1: (), 500: ()})
    assert coverage.compute_volume(roster, frames)["span"]["frame_count"] == 500


def test_large_500_frame_volume_is_fast():
    roster = [str(i + 1) for i in range(500)]
    frames = [coverage.Frame(frame_no=i + 1, candidates=()) for i in range(500)]
    frames[0] = coverage.Frame(frame_no=1, candidates=("1",))
    frames[-1] = coverage.Frame(frame_no=500, candidates=("500",))
    # 500 帧、500 版，首末钉死，逐帧只能前进 1，于是每帧恰好对一版。
    result = coverage.compute_volume(roster, frames)
    assert result["span"]["frame_count"] == 500
    for plate in ("1", "2", "250", "500"):
        assert result["pages"][plate]["guaranteed"] is True
        assert result["pages"][plate]["possible_scanned_frames"] == [int(plate)]


# ---------------------------------------------------------------------------
# API 测试
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def submit(client, issue, code, frames, plates=None):
    body = {"volume_code": code, "frames": frames}
    if plates is not None:
        body["plates"] = plates
    return client.post(f"/issues/{issue}/volumes", json=body)


def test_submit_and_check_flow(client):
    plates = ["1", "2", "3", "4"]
    r = submit(
        client,
        "2026-11-22",
        "A",
        [
            {"frame_no": 1, "candidates": ["2"]},
            {"frame_no": 2, "candidates": []},
        ],
        plates,
    )
    assert r.status_code == 201, r.text
    assert r.json()["issue_created"] is True

    # 帧1=版2；帧2 认不出，可以是版2或版3。
    # 版1、4 不可能由本卷已扫帧拍到 -> 缺扫且无漏扫帧线索。
    report = client.get("/issues/2026-11-22/plates").json()
    status = {p["plate"]: p["status"] for p in report["plates"]}
    assert status == {"1": "missing", "2": "scanned", "3": "pending", "4": "missing"}
    assert report["summary"] == {"plate_count": 4, "scanned": 1, "pending": 1, "missing": 2}

    missing4 = next(p for p in report["plates"] if p["plate"] == "4")
    assert missing4["possible_volumes_and_missed_frames"] == []
    assert missing4["hint"]


def test_pending_plate_lists_candidates(client):
    plates = ["1", "2", "3"]
    submit(client, "I", "A", [
        {"frame_no": 10, "candidates": []},
        {"frame_no": 11, "candidates": ["2", "3"]},
    ], plates)
    report = client.get("/issues/I/plates").json()
    by = {p["plate"]: p for p in report["plates"]}

    # 可行排法：12、22、23、33。版1 只在 12 中出现，版2 在 33 中没有，
    # 版3 只在 23、33 中出现。
    assert by["1"]["status"] == "pending"
    assert by["1"]["possible_volumes_and_frames"] == [
        {"volume_code": "A", "frame_no": 10},
    ]
    assert by["2"]["status"] == "pending"
    assert by["2"]["possible_volumes_and_frames"] == [
        {"volume_code": "A", "frame_no": 10},
        {"volume_code": "A", "frame_no": 11},
    ]
    assert by["3"]["status"] == "pending"
    assert by["3"]["possible_volumes_and_frames"] == [
        {"volume_code": "A", "frame_no": 10},
        {"volume_code": "A", "frame_no": 11},
    ]


def test_pending_from_multiple_volumes(client):
    submit(client, "I", "A", [
        {"frame_no": 1, "candidates": ["1", "2"]},
        {"frame_no": 2, "candidates": ["2"]},
    ], ["1", "2", "3"])
    submit(client, "I", "B", [
        {"frame_no": 5, "candidates": ["1", "2"]},
    ])
    report = client.get("/issues/I/plates").json()
    by = {p["plate"]: p for p in report["plates"]}
    # 版1 在两卷里都只在部分排法下被拍到，线索要合并列出。
    assert by["1"]["status"] == "pending"
    assert by["1"]["possible_volumes_and_frames"] == [
        {"volume_code": "A", "frame_no": 1},
        {"volume_code": "B", "frame_no": 5},
    ]
    assert by["2"]["status"] == "scanned"


def test_missing_lists_volume_and_missed_frame(client):
    plates = ["1", "2", "3", "4", "5"]
    submit(client, "I", "A", [
        {"frame_no": 1, "candidates": ["1"]},
        {"frame_no": 3, "candidates": ["3"]},
    ], plates)
    report = client.get("/issues/I/plates").json()
    by = {p["plate"]: p for p in report["plates"]}
    # 唯一排法 123：漏扫帧 2 只能落在版2。
    assert by["2"]["status"] == "missing"
    assert by["2"]["possible_volumes_and_missed_frames"] == [
        {"volume_code": "A", "frame_no": 2}
    ]
    assert by["2"]["hint"] is None
    # 版4、5 连漏扫帧也够不着 -> 提示另找胶片。
    for plate in ("4", "5"):
        assert by[plate]["status"] == "missing"
        assert by[plate]["possible_volumes_and_missed_frames"] == []
        assert by[plate]["hint"]


def test_new_volume_only_recomputes_itself(client, tmp_path):
    plates = ["1", "2", "3"]
    submit(client, "I", "A", [{"frame_no": 1, "candidates": ["1"]}], plates)
    before = client.get("/issues/I/volumes/A").json()

    r = submit(client, "I", "B", [{"frame_no": 5, "candidates": []}])
    assert r.status_code == 201, r.text
    assert r.json()["roster_changed"] is False
    after = client.get("/issues/I/volumes/A").json()
    assert after["updated_at"] == before["updated_at"]


def test_roster_change_recomputes_all_volumes(client):
    submit(client, "I", "A", [{"frame_no": 1, "candidates": ["1"]}], ["1", "2"])
    r = submit(client, "I", "B", [{"frame_no": 3, "candidates": ["3"]}], ["1", "2", "3"])
    assert r.status_code == 201, r.text
    assert r.json()["roster_changed"] is True
    assert r.json()["recomputed_volume_codes"] == ["A"]
    a = client.get("/issues/I/volumes/A").json()
    assert set(a["pages"]) == {"1", "2", "3"}


def test_infeasible_roster_change_rolls_back_everything(client):
    submit(client, "I", "A", [{"frame_no": 1, "candidates": ["3"]}], ["1", "2", "3"])
    before_issue = client.get("/issues/I").json()
    before_a = client.get("/issues/I/volumes/A").json()

    # 新清单只有 1、2，卷 A 的帧候选 3 失效 -> 整体回滚
    r = submit(client, "I", "B", [{"frame_no": 5, "candidates": ["1"]}], ["1", "2"])
    assert r.status_code == 422
    assert r.json()["error"]["data_unchanged"] is True

    assert client.get("/issues/I").json()["plates"] == before_issue["plates"]
    assert client.get("/issues/I/volumes/A").json()["frames"] == before_a["frames"]
    assert client.get("/issues/I/volumes/B").status_code == 404


def test_infeasible_new_issue_is_not_created(client):
    r = submit(client, "J", "A", [{"frame_no": 2, "candidates": ["3"]},
                                  {"frame_no": 1, "candidates": ["1"]}],
               ["1", "2", "3"])
    assert r.status_code == 422
    assert client.get("/issues/J").status_code == 404


def test_resubmitting_same_volume_uses_new_frames(client):
    submit(client, "I", "A", [{"frame_no": 1, "candidates": ["1"]}], ["1", "2"])
    r = submit(client, "I", "A", [{"frame_no": 1, "candidates": ["2"]}])
    assert r.status_code == 201
    a = client.get("/issues/I/volumes/A").json()
    assert a["frames"] == [{"frame_no": 1, "candidates": ["2"]}]


def test_invalid_inputs(client):
    base = [{"frame_no": 1, "candidates": ["1"]}]
    assert submit(client, "I", "A", base).status_code == 422  # 首期缺清单
    assert submit(client, "I", "A", base, []).status_code == 422
    assert submit(client, "I", "A", base, ["1", "1"]).status_code == 422
    assert submit(client, "I", "A", [{"frame_no": 1, "candidates": ["9"]}], ["1"]).status_code == 422
    assert submit(client, "I", "A", [{"frame_no": 0, "candidates": ["1"]}], ["1"]).status_code == 422
    assert submit(client, "I", "A", [
        {"frame_no": 1, "candidates": ["1"]},
        {"frame_no": 1, "candidates": ["1"]},
    ], ["1"]).status_code == 422
    assert submit(client, "I", "A", [{"frame_no": 1, "candidates": ["1"]}], ["1"]).status_code == 201


def test_missing_issue_and_plate_404(client):
    submit(client, "I", "A", [{"frame_no": 1, "candidates": ["1"]}], ["1", "2"])
    assert client.get("/issues/NOPE/plates").status_code == 404
    assert client.get("/issues/I/plates/9").status_code == 404
    assert client.get("/issues/I/volumes/Z").status_code == 404


def test_db_persists_across_app_restart(tmp_path):
    db = str(tmp_path / "persist.db")
    with TestClient(create_app(db)) as c:
        assert submit(c, "I", "A", [{"frame_no": 1, "candidates": ["1"]}], ["1"]).status_code == 201
    with TestClient(create_app(db)) as c:
        assert c.get("/issues/I/volumes/A").status_code == 200
        assert c.get("/issues/I/plates").json()["summary"]["scanned"] == 1


# ---------------------------------------------------------------------------
# 存储失败回滚
# ---------------------------------------------------------------------------


def test_write_failure_rolls_back_and_keeps_old_data(client, monkeypatch):
    import sqlite3

    from app import db as db_module

    submit(client, "I", "A", [{"frame_no": 1, "candidates": ["1"]}], ["1", "2"])

    calls = {"n": 0}
    real = db_module._frames_to_json

    def flaky(frames):
        calls["n"] += 1
        if calls["n"] >= 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real(frames)

    monkeypatch.setattr(db_module, "_frames_to_json", flaky)

    r = submit(client, "I", "B", [{"frame_no": 3, "candidates": ["2"]}], ["1", "2", "3"])
    assert r.status_code == 503
    assert r.json()["error"]["data_unchanged"] is True

    monkeypatch.setattr(db_module, "_frames_to_json", real)
    # 清单、已有卷都保持提交前状态，新卷没有落库。
    assert client.get("/issues/I").json()["plates"] == ["1", "2"]
    assert set(client.get("/issues/I/volumes/A").json()["pages"]) == {"1", "2"}
    assert client.get("/issues/I/volumes/B").status_code == 404


def test_blank_or_non_string_plate_rejected(client):
    body = {
        "volume_code": "A",
        "plates": ["1", "  "],
        "frames": [{"frame_no": 1, "candidates": []}],
    }
    r = client.post("/issues/I/volumes", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] in {"invalid_plate", "empty_roster"}
    assert client.get("/issues/I").status_code == 404


def test_plate_order_is_preserved(client):
    plates = ["A", "B", "C"]
    submit(client, "I", "A", [{"frame_no": 1, "candidates": ["A"]}], plates)
    report = client.get("/issues/I/plates").json()
    assert [p["plate"] for p in report["plates"]] == plates
    assert client.get("/issues/I").json()["plates"] == plates


def test_set_roster_endpoint_recomputes_and_rolls_back(client):
    submit(client, "I", "A", [{"frame_no": 1, "candidates": ["1"]}], ["1", "2"])

    ok = client.put("/issues/I/plates", json={"plates": ["1", "2", "3"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["recomputed_volume_codes"] == ["A"]
    assert set(client.get("/issues/I/volumes/A").json()["pages"]) == {"1", "2", "3"}

    # 新清单去掉卷 A 用到的候选版 1 -> 排不通，全部保留旧状态。
    bad = client.put("/issues/I/plates", json={"plates": ["2", "3"]})
    assert bad.status_code == 422
    assert bad.json()["error"]["data_unchanged"] is True
    assert client.get("/issues/I").json()["plates"] == ["1", "2", "3"]
    assert set(client.get("/issues/I/volumes/A").json()["pages"]) == {"1", "2", "3"}
