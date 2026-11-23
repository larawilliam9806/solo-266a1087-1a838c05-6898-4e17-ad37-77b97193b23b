"""按期汇总各版核查结论。"""

from __future__ import annotations

from typing import Any

from . import coverage


HINT_NO_FILM = "现有卷内没有可能拍到该版的漏扫帧，请另找胶片。"


def build_issue_report(
    issue: dict[str, Any],
    volumes: list[dict[str, Any]],
) -> dict[str, Any]:
    """把各卷的排法结果汇总成“按期查各版”的清单。

    判定规则（对所有卷的所有可行排法取并集/交集）：

    * 每种可行排法都有已扫帧拍到该版 -> 已扫；
    * 只有部分排法有已扫帧拍到该版 -> 待核；
    * 没有任何排法能由已扫帧拍到该版 -> 缺扫。
    """

    roster: list[str] = issue["plates"]

    guaranteed_by: dict[str, list[str]] = {plate: [] for plate in roster}
    scanned_options: dict[str, list[dict[str, Any]]] = {plate: [] for plate in roster}
    missed_options: dict[str, list[dict[str, Any]]] = {plate: [] for plate in roster}

    for volume in volumes:
        code = volume["volume_code"]
        pages = volume["result"]["pages"]
        for plate in roster:
            page = pages.get(plate)
            if page is None:  # 卷结果由旧版数据结构产生时的兜底
                continue
            if page["guaranteed"]:
                guaranteed_by[plate].append(code)
            for frame_no in page["possible_scanned_frames"]:
                scanned_options[plate].append(
                    {"volume_code": code, "frame_no": frame_no}
                )
            for frame_no in page["possible_missed_frames"]:
                missed_options[plate].append(
                    {"volume_code": code, "frame_no": frame_no}
                )

    plate_reports: list[dict[str, Any]] = []
    summary = {
        coverage.STATUS_SCANNED: 0,
        coverage.STATUS_PENDING: 0,
        coverage.STATUS_MISSING: 0,
    }

    for plate in roster:
        if guaranteed_by[plate]:
            status = coverage.STATUS_SCANNED
        elif scanned_options[plate]:
            status = coverage.STATUS_PENDING
        else:
            status = coverage.STATUS_MISSING

        report: dict[str, Any] = {
            "plate": plate,
            "status": status,
            "status_label": coverage.STATUS_LABELS[status],
        }
        if status == coverage.STATUS_SCANNED:
            report["guaranteed_by_volumes"] = guaranteed_by[plate]
            report["scanned_frames"] = scanned_options[plate]
        elif status == coverage.STATUS_PENDING:
            report["possible_volumes_and_frames"] = scanned_options[plate]
        else:
            report["possible_volumes_and_missed_frames"] = missed_options[plate]
            report["hint"] = None if missed_options[plate] else HINT_NO_FILM

        plate_reports.append(report)
        summary[status] += 1

    updated_at = issue["updated_at"]
    for volume in volumes:
        if volume["updated_at"] > updated_at:
            updated_at = volume["updated_at"]

    return {
        "issue_id": issue["issue_id"],
        "plates": plate_reports,
        "summary": {
            "plate_count": len(roster),
            coverage.STATUS_SCANNED: summary[coverage.STATUS_SCANNED],
            coverage.STATUS_PENDING: summary[coverage.STATUS_PENDING],
            coverage.STATUS_MISSING: summary[coverage.STATUS_MISSING],
        },
        "volumes": [
            {
                "volume_code": volume["volume_code"],
                "span": volume["result"]["span"],
            }
            for volume in volumes
        ],
        "updated_at": updated_at,
    }
