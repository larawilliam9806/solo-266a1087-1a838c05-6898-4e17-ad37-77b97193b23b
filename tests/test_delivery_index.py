"""交档索引：核定、整期替换、变更后复检与撤下。"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


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


def approve(client, issue, payload):
    return client.put(f"/issues/{issue}/delivery-index", json=payload)


def entries(*triples):
    return {
        "entries": [
            {"plate": plate, "volume_code": volume, "frame_no": frame}
            for plate, volume, frame in triples
        ]
    }


def seed_two_volumes(client, issue="I"):
    """卷 A 钉死版 1、2；卷 B 钉死版 3、4。"""

    assert submit(
        client,
        issue,
        "A",
        [
            {"frame_no": 1, "candidates": ["1"]},
            {"frame_no": 2, "candidates": ["2"]},
        ],
        ["1", "2", "3", "4"],
    ).status_code == 201
    assert submit(
        client,
        issue,
        "B",
        [
            {"frame_no": 10, "candidates": ["3"]},
            {"frame_no": 11, "candidates": ["4"]},
        ],
    ).status_code == 201


def test_approve_and_query_index(client):
    seed_two_volumes(client)

    r = approve(
        client,
        "I",
        entries(("1", "A", 1), ("2", "A", 2), ("3", "B", 10), ("4", "B", 11)),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved"
    assert body["serving"] is True
    assert [e["plate"] for e in body["entries"]] == ["1", "2", "3", "4"]
    assert body["index"]["3"] == {"volume_code": "B", "frame_no": 10}

    got = client.get("/issues/I/delivery-index")
    assert got.status_code == 200
    assert got.json()["index"] == body["index"]
    assert client.get("/issues/I").json()["delivery_index_status"] == "approved"
    listed = client.get("/delivery-indexes").json()["indexes"]
    assert listed[0]["issue_id"] == "I"


def test_alternate_payload_shapes(client):
    seed_two_volumes(client)

    as_mapping = {
        "1": {"volume_code": "A", "frame_no": 1},
        "2": {"volume_code": "A", "frame_no": 2},
        "3": {"volume_code": "B", "frame_no": 10},
        "4": {"volume_code": "B", "frame_no": 11},
    }
    assert approve(client, "I", as_mapping).status_code == 200

    as_list = [
        {"plate": "1", "volume": "A", "frame": 1},
        {"page": "2", "volume_code": "A", "frame_no": 2},
        {"plate": "3", "volume_code": "B", "frame_no": 10},
        {"plate": "4", "volume_code": "B", "frame_no": 11},
    ]
    assert approve(client, "I", as_list).status_code == 200

    compact = {"index": {"1": "A:1", "2": "A:2", "3": "B:10", "4": "B:11"}}
    assert approve(client, "I", compact).status_code == 200

    as_pairs = {
        "picks": [
            ["1", "A", 1],
            ["2", "A", 2],
            ["3", "B", 10],
            ["4", "B", 11],
        ]
    }
    assert approve(client, "I", as_pairs).status_code == 200

    got = client.get("/issues/I/delivery-index").json()
    assert got["index"]["4"] == {"volume_code": "B", "frame_no": 11}


def test_each_plate_exactly_once_and_all_plates_covered(client):
    seed_two_volumes(client)

    dup_plate = client.put(
        "/issues/I/delivery-index",
        json={
            "entries": [
                {"plate": "1", "volume_code": "A", "frame_no": 1},
                {"plate": "1", "volume_code": "A", "frame_no": 2},
                {"plate": "2", "volume_code": "A", "frame_no": 2},
                {"plate": "3", "volume_code": "B", "frame_no": 10},
                {"plate": "4", "volume_code": "B", "frame_no": 11},
            ]
        },
    )
    assert dup_plate.status_code == 422
    assert dup_plate.json()["error"]["code"] == "duplicate_plate"

    missing = approve(
        client, "I", entries(("1", "A", 1), ("2", "A", 2), ("3", "B", 10))
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "missing_plate"

    assert client.get("/issues/I/delivery-index").status_code == 404


def test_same_frame_cannot_serve_two_plates(client):
    seed_two_volumes(client)
    r = approve(
        client,
        "I",
        entries(("1", "A", 1), ("2", "A", 1), ("3", "B", 10), ("4", "B", 11)),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "duplicate_frame_no"
    assert client.get("/issues/I/delivery-index").status_code == 404


def test_unknown_volume_and_frame_rejected(client):
    seed_two_volumes(client)
    bad_volume = approve(
        client,
        "I",
        entries(("1", "A", 1), ("2", "A", 2), ("3", "Z", 10), ("4", "B", 11)),
    )
    assert bad_volume.status_code == 422
    assert bad_volume.json()["error"]["code"] == "unknown_volume"

    # 帧 12 不在卷 B 的已扫帧里（在 10..11 的跨度内但未提交）。
    bad_frame = approve(
        client,
        "I",
        entries(("1", "A", 1), ("2", "A", 2), ("3", "B", 12), ("4", "B", 11)),
    )
    assert bad_frame.status_code == 422
    assert bad_frame.json()["error"]["code"] == "unknown_frame_no"


def test_picks_must_share_one_arrangement(client):
    # 卷 A 三个已扫帧都认不出（各自可落任意版），但版序只能单调不减。
    assert submit(
        client,
        "I",
        "A",
        [
            {"frame_no": 1, "candidates": []},
            {"frame_no": 2, "candidates": []},
            {"frame_no": 3, "candidates": []},
        ],
        ["1", "2", "3"],
    ).status_code == 201

    # 单独看每条都可行：帧1 可落版3、帧2 可落版2、帧3 可落版1。
    # 合在一起则要求版序 3、2、1 递减，没有任何一套排法能同时满足。
    clash = approve(
        client,
        "I",
        entries(("3", "A", 1), ("2", "A", 2), ("1", "A", 3)),
    )
    assert clash.status_code == 422
    assert clash.json()["error"]["code"] == "infeasible_picks"
    assert client.get("/issues/I/delivery-index").status_code == 404

    # 单调递增的同类选择则可行。
    ok = approve(client, "I", entries(("1", "A", 1), ("2", "A", 2), ("3", "A", 3)))
    assert ok.status_code == 200, ok.text


def test_approval_replaces_previous_index(client):
    seed_two_volumes(client)
    first = approve(
        client,
        "I",
        entries(("1", "A", 1), ("2", "A", 2), ("3", "B", 10), ("4", "B", 11)),
    )
    assert first.status_code == 200
    old_approved_at = first.json()["approved_at"]

    second = approve(
        client,
        "I",
        entries(("1", "A", 1), ("2", "A", 1 + 1), ("3", "B", 10), ("4", "B", 11)),
    )
    assert second.status_code == 200
    got = client.get("/issues/I/delivery-index").json()
    assert got["approved_at"] != old_approved_at
    assert len(got["entries"]) == 4

    # 核定失败时旧索引原样保留（仍供档）。
    bad = approve(
        client,
        "I",
        entries(("1", "A", 1), ("2", "A", 2), ("3", "B", 10)),
    )
    assert bad.status_code == 422
    kept = client.get("/issues/I/delivery-index").json()
    assert kept["status"] == "approved"
    assert kept["entries"] == second.json()["entries"]
    assert kept["approved_at"] == second.json()["approved_at"]


def test_volume_change_withdraws_index_and_requires_reapproval(client):
    seed_two_volumes(client)
    assert approve(
        client,
        "I",
        entries(("1", "A", 1), ("2", "A", 2), ("3", "B", 10), ("4", "B", 11)),
    ).status_code == 200

    # 换卷 B：帧 10 的候选从版 3 改成版 4 -> 原索引里的 B:10 不再可能拍到版 3。
    r = submit(
        client,
        "I",
        "B",
        [
            {"frame_no": 10, "candidates": ["4"]},
            {"frame_no": 11, "candidates": ["4"]},
        ],
    )
    assert r.status_code == 201
    assert r.json()["delivery_index_status"] == "withdrawn"

    got = client.get("/issues/I/delivery-index").json()
    assert got["status"] == "withdrawn"
    assert got["serving"] is False
    assert got["reason"]

    # 数据改回原样也不会自动恢复，必须重新核定。
    r = submit(
        client,
        "I",
        "B",
        [
            {"frame_no": 10, "candidates": ["3"]},
            {"frame_no": 11, "candidates": ["4"]},
        ],
    )
    assert r.status_code == 201
    assert client.get("/issues/I/delivery-index").json()["status"] == "withdrawn"

    again = approve(
        client,
        "I",
        entries(("1", "A", 1), ("2", "A", 2), ("3", "B", 10), ("4", "B", 11)),
    )
    assert again.status_code == 200
    assert again.json()["status"] == "approved"


def test_still_valid_index_keeps_serving_after_change(client):
    seed_two_volumes(client)
    assert approve(
        client,
        "I",
        entries(("1", "A", 1), ("2", "A", 2), ("3", "B", 10), ("4", "B", 11)),
    ).status_code == 200

    # 给卷 A 补一个不影响所选帧的后续帧：所选 A:1、A:2 仍可同排。
    r = submit(
        client,
        "I",
        "A",
        [
            {"frame_no": 1, "candidates": ["1"]},
            {"frame_no": 2, "candidates": ["2"]},
            {"frame_no": 3, "candidates": ["2"]},
        ],
    )
    assert r.status_code == 201
    assert r.json()["delivery_index_status"] == "approved"
    assert client.get("/issues/I/delivery-index").json()["status"] == "approved"


def test_roster_change_revalidates_index(client):
    seed_two_volumes(client)
    assert approve(
        client,
        "I",
        entries(("1", "A", 1), ("2", "A", 2), ("3", "B", 10), ("4", "B", 11)),
    ).status_code == 200

    # 加一版 5：原索引没覆盖新清单 -> 撤下。
    r = client.put("/issues/I/plates", json={"plates": ["1", "2", "3", "4", "5"]})
    assert r.status_code == 200
    assert client.get("/issues/I/delivery-index").json()["status"] == "withdrawn"

    # 再补上版 5 并重新核定。
    assert submit(
        client,
        "I",
        "C",
        [{"frame_no": 30, "candidates": ["5"]}],
    ).status_code == 201
    again = approve(
        client,
        "I",
        entries(
            ("1", "A", 1),
            ("2", "A", 2),
            ("3", "B", 10),
            ("4", "B", 11),
            ("5", "C", 30),
        ),
    )
    assert again.status_code == 200

    # 仅调整版序（版号集合不变）：索引仍有效，继续供档。
    reordered = client.put(
        "/issues/I/plates", json={"plates": ["5", "1", "2", "3", "4"]}
    )
    assert reordered.status_code == 200, reordered.text
    assert client.get("/issues/I/delivery-index").json()["status"] == "approved"


def test_failed_roster_change_keeps_index_and_check_data(client):
    seed_two_volumes(client)
    assert approve(
        client,
        "I",
        entries(("1", "A", 1), ("2", "A", 2), ("3", "B", 10), ("4", "B", 11)),
    ).status_code == 200
    before = client.get("/issues/I/plates").json()

    # 新清单去掉版 3、4，卷 B 的候选失效 -> 整次变更回滚，索引仍供档。
    bad = client.put("/issues/I/plates", json={"plates": ["1", "2"]})
    assert bad.status_code == 422
    assert bad.json()["error"]["data_unchanged"] is True
    assert client.get("/issues/I/plates").json() == before
    assert client.get("/issues/I/delivery-index").json()["status"] == "approved"


def test_write_failure_keeps_old_index(client, monkeypatch):
    seed_two_volumes(client)
    assert approve(
        client,
        "I",
        entries(("1", "A", 1), ("2", "A", 2), ("3", "B", 10), ("4", "B", 11)),
    ).status_code == 200
    before = client.get("/issues/I/delivery-index").json()

    from app import db as db_module

    real_json = db_module._json

    def flaky(value):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db_module, "_json", flaky)

    # 条目与旧索引一致，但写入必然失败；旧索引与核定时间都不动。
    r = approve(
        client,
        "I",
        entries(("1", "A", 1), ("2", "A", 2), ("3", "B", 10), ("4", "B", 11)),
    )
    assert r.status_code == 503
    assert r.json()["error"]["data_unchanged"] is True

    monkeypatch.setattr(db_module, "_json", real_json)
    kept = client.get("/issues/I/delivery-index").json()
    assert kept["entries"] == before["entries"]
    assert kept["approved_at"] == before["approved_at"]
    assert kept["status"] == "approved"


def test_unknown_issue_and_index_404(client):
    assert approve(client, "NOPE", entries(("1", "A", 1))).status_code == 404
    assert client.get("/issues/NOPE/delivery-index").status_code == 404
    seed_two_volumes(client, "J")
    assert client.get("/issues/J/delivery-index").status_code == 404


def test_empty_and_malformed_payloads(client):
    seed_two_volumes(client)
    assert approve(client, "I", {"entries": []}).status_code == 422
    assert approve(client, "I", entries()).status_code == 422
    r = client.put("/issues/I/delivery-index", json={"entries": [{"plate": "1"}]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_index"
    r = client.put(
        "/issues/I/delivery-index",
        json={"entries": [{"plate": "9", "volume_code": "A", "frame_no": 1}]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] in {"unknown_plate", "missing_plate"}


def test_index_persists_across_restart(tmp_path):
    db = str(tmp_path / "persist.db")
    with TestClient(create_app(db)) as c:
        seed_two_volumes(c)
        assert approve(
            c,
            "I",
            entries(("1", "A", 1), ("2", "A", 2), ("3", "B", 10), ("4", "B", 11)),
        ).status_code == 200
    with TestClient(create_app(db)) as c:
        got = c.get("/issues/I/delivery-index")
        assert got.status_code == 200
        assert got.json()["status"] == "approved"
        assert got.json()["index"]["2"] == {"volume_code": "A", "frame_no": 2}


def test_feasible_with_picks_matches_brute_force():
    import itertools
    import random

    from app import coverage

    rng = random.Random(20261123)
    for _ in range(200):
        roster = [str(i + 1) for i in range(rng.randint(2, 4))]
        span = rng.randint(1, 5)
        frames = []
        for pos in range(span):
            if rng.random() < 0.3:
                continue
            k = rng.randint(0, 2)
            cands = tuple(rng.sample(roster, k)) if k < len(roster) else tuple(roster)
            frames.append(coverage.Frame(frame_no=100 + pos, candidates=cands))
        if not frames:
            continue

        # 随机挑一两个已扫帧与目标版。
        picks = {}
        for frame in rng.sample(frames, rng.randint(0, min(2, len(frames)))):
            picks[frame.frame_no] = rng.choice(roster)

        first = frames[0].frame_no
        scanned = {f.frame_no: f for f in frames}
        expected = False
        for assignment in itertools.product(range(len(roster)), repeat=span):
            ok = True
            for pos in range(span):
                frame = scanned.get(first + pos)
                if frame is not None and frame.candidates:
                    if roster[assignment[pos]] not in frame.candidates:
                        ok = False
                        break
                if pos and assignment[pos] - assignment[pos - 1] not in (0, 1):
                    ok = False
                    break
                if frame is not None and frame.frame_no in picks:
                    if roster[assignment[pos]] != picks[frame.frame_no]:
                        ok = False
                        break
            if ok:
                expected = True
                break

        try:
            got = coverage.feasible_with_picks(roster, frames, picks)
        except coverage.ArrangementError:
            got = False
        assert got == expected, (roster, frames, picks)


def test_failed_reapproval_keeps_withdrawn_state(client):
    seed_two_volumes(client)
    assert approve(
        client,
        "I",
        entries(("1", "A", 1), ("2", "A", 2), ("3", "B", 10), ("4", "B", 11)),
    ).status_code == 200

    # 先让索引失效撤下。
    assert submit(
        client,
        "I",
        "B",
        [
            {"frame_no": 10, "candidates": ["4"]},
            {"frame_no": 11, "candidates": ["4"]},
        ],
    ).status_code == 201
    withdrawn = client.get("/issues/I/delivery-index").json()
    assert withdrawn["status"] == "withdrawn"

    # 仍按旧的（已失效）对应表重新核定 -> 422，撤下状态与原因保持不变。
    bad = approve(
        client,
        "I",
        entries(("1", "A", 1), ("2", "A", 2), ("3", "B", 10), ("4", "B", 11)),
    )
    assert bad.status_code == 422
    after = client.get("/issues/I/delivery-index").json()
    assert after["status"] == "withdrawn"
    assert after["reason"] == withdrawn["reason"]
    assert after["approved_at"] == withdrawn["approved_at"]


def test_adding_unrelated_volume_keeps_index_serving(client):
    seed_two_volumes(client)
    assert approve(
        client,
        "I",
        entries(("1", "A", 1), ("2", "A", 2), ("3", "B", 10), ("4", "B", 11)),
    ).status_code == 200
    r = submit(client, "I", "C", [{"frame_no": 50, "candidates": []}])
    assert r.status_code == 201
    assert r.json()["delivery_index_status"] == "approved"
    assert client.get("/issues/I/delivery-index").json()["status"] == "approved"
