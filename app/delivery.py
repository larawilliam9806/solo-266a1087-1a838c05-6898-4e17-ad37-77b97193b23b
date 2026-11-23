"""交档索引：逐版取图的提交、核定与失效校验。

交档索引是一张“版号 -> (卷号, 原帧号)”的对应表。核定条件：

* 必须覆盖该期的全部版号，且每版恰好一个条目；
* 所选原帧必须是该卷已提交的已扫帧；
* 同一卷里的同一原帧不能同时挂到两个版；
* 对同一卷，所有被选中的帧必须出现在同一套合法排法里。

索引核定通过后替换该期旧索引。卷数据或全期清单变化时，用更新后的数据
重新校验原索引：仍满足条件就继续供档，否则随本次变更撤下（不自动恢复，
必须重新核定）。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from . import coverage

# 请求体里常见的容器字段名。既支持 {"entries": [...]}，也支持直接提交
# 列表或 {"版号": {"volume_code": ..., "frame_no": ...}} 的映射。
_CONTAINER_KEYS = (
    "entries",
    "index",
    "items",
    "assignments",
    "picks",
    "mapping",
    "plates",
    "rows",
    "records",
    "table",
    "selection",
    "selections",
    "交档索引",
    "对应表",
    "逐版取图",
)

_PLATE_KEYS = ("plate", "plate_no", "plate_number", "page", "page_no", "版号")
_VOLUME_KEYS = ("volume_code", "volume", "volume_no", "volume_id", "卷号")
_FRAME_KEYS = ("frame_no", "frame", "frame_number", "original_frame_no", "原帧号")


def _invalid(message: str, code: str = "invalid_index") -> None:
    raise coverage.ArrangementError(message, code)


def _pick(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _text(value: Any, field: str, code: str) -> str:
    if isinstance(value, bool) or value is None:
        _invalid(f"交档索引的{field}不能为空", code)
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        _invalid(f"交档索引的{field}必须是字符串，收到 {value!r}", code)
    text = value.strip()
    if not text:
        _invalid(f"交档索引的{field}不能为空", code)
    return text


def _frame_no(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        _invalid("交档索引的原帧号必须为正整数", "invalid_frame_no")
    if isinstance(value, int):
        frame_no = value
    elif isinstance(value, float) and value.is_integer():
        frame_no = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            _invalid(f"交档索引的原帧号必须为正整数，收到 {value!r}", "invalid_frame_no")
        frame_no = int(text)
    else:
        _invalid(f"交档索引的原帧号必须为正整数，收到 {value!r}", "invalid_frame_no")
    if frame_no < 1:
        _invalid(f"交档索引的原帧号必须为正整数，收到 {frame_no!r}", "invalid_frame_no")
    return frame_no


def _scalar_entry(plate: str, text: str) -> dict[str, Any]:
    """解析 ``"V01:1001"`` 这类紧凑写法。"""

    for separator in ("#", ":", "/", "@", ","):
        if separator in text:
            volume, frame = text.split(separator, 1)
            return {
                "plate": _text(plate, "版号", "invalid_plate"),
                "volume_code": _text(volume, "卷号", "invalid_volume"),
                "frame_no": _frame_no(frame),
            }
    _invalid(
        f"版号 {plate!r} 的交档条目缺少卷号或原帧号（应形如 'V01:1001'）",
        "invalid_index",
    )


def _entry(raw: Any, plate_hint: Any = None) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        plate = _pick(raw, _PLATE_KEYS)
        if plate is None:
            plate = plate_hint
        volume = _pick(raw, _VOLUME_KEYS)
        frame = _pick(raw, _FRAME_KEYS)
        if volume is None or frame is None:
            _invalid("每条交档索引都必须包含卷号和原帧号", "invalid_index")
        return {
            "plate": _text(plate, "版号", "invalid_plate"),
            "volume_code": _text(volume, "卷号", "invalid_volume"),
            "frame_no": _frame_no(frame),
        }

    if isinstance(raw, (list, tuple)):
        if len(raw) < 3:
            _invalid("交档索引条目必须包含版号、卷号和原帧号", "invalid_index")
        return {
            "plate": _text(raw[0], "版号", "invalid_plate"),
            "volume_code": _text(raw[1], "卷号", "invalid_volume"),
            "frame_no": _frame_no(raw[2]),
        }

    if isinstance(raw, str) and plate_hint is not None:
        return _scalar_entry(_text(plate_hint, "版号", "invalid_plate"), raw)

    _invalid(f"无法识别的交档索引条目 {raw!r}", "invalid_index")


def _entries_from_container(container: Any) -> list[dict[str, Any]]:
    if isinstance(container, Mapping):
        entries: list[dict[str, Any]] = []
        for plate_key, value in container.items():
            if isinstance(value, Mapping):
                entries.append(_entry(value, plate_hint=plate_key))
            elif isinstance(value, (list, tuple)):
                if len(value) == 2:
                    entries.append(_entry([plate_key, value[0], value[1]]))
                else:
                    entries.append(_entry(value, plate_hint=plate_key))
            elif isinstance(value, str):
                entries.append(_scalar_entry(str(plate_key).strip(), value))
            else:
                _invalid(
                    f"版号 {plate_key!r} 的交档条目格式不正确", "invalid_index"
                )
        return entries

    if isinstance(container, (list, tuple)):
        return [_entry(item) for item in container]

    _invalid("交档索引必须是对应表（列表或版号到卷号/原帧号的映射）", "invalid_index")


def normalize_entries(payload: Any) -> list[dict[str, Any]]:
    """把各种提交形态整理成 ``[{plate, volume_code, frame_no}, ...]``。

    支持：

    * ``{"entries": [...]}``（``index``、``items``、``picks`` 等别名同理）；
    * 直接提交条目列表；
    * ``{"1": {"volume_code": "V01", "frame_no": 1001}, ...}``；
    * 单条 ``{"plate": "1", "volume_code": "V01", "frame_no": 1001}``。
    """

    if payload is None:
        _invalid("交档索引不能为空", "empty_index")

    if isinstance(payload, Mapping):
        # 单条条目直接作为请求体。
        if _pick(payload, _PLATE_KEYS) is not None and _pick(payload, _FRAME_KEYS) is not None:
            return [_entry(payload)]

        for key in _CONTAINER_KEYS:
            if key in payload:
                return _entries_from_container(payload[key])

        # 没有已知容器字段时，把整个对象当作“版号 -> 条目”的映射。
        return _entries_from_container(payload)

    if isinstance(payload, (list, tuple)):
        return _entries_from_container(payload)

    _invalid(f"无法识别的交档索引请求体 {payload!r}", "invalid_index")


def _volume_frames(volume: Mapping[str, Any]) -> list[coverage.Frame]:
    return [
        coverage.Frame(
            frame_no=int(item["frame_no"]),
            candidates=tuple(item.get("candidates") or ()),
        )
        for item in sorted(volume["frames"], key=lambda item: int(item["frame_no"]))
    ]


def validate_index(
    roster: Sequence[str],
    volumes: Iterable[Mapping[str, Any]],
    payload: Any,
) -> list[dict[str, Any]]:
    """按核定条件校验交档索引，通过后返回按版序排好的条目。

    任何不合格都会抛出 :class:`coverage.ArrangementError`。``payload`` 可以是
    原始请求体，也可以是数据库里存下来的条目列表。
    """

    roster = list(roster)
    roster_set = set(roster)
    entries = normalize_entries(payload)
    if not entries:
        _invalid("交档索引不能为空", "empty_index")

    volume_map = {volume["volume_code"]: volume for volume in volumes}
    frames_by_volume: dict[str, set[int]] = {
        code: {int(frame["frame_no"]) for frame in volume["frames"]}
        for code, volume in volume_map.items()
    }

    by_plate: dict[str, dict[str, Any]] = {}
    used_frames: set[tuple[str, int]] = set()

    for item in entries:
        plate = item["plate"]
        volume_code = item["volume_code"]
        frame_no = item["frame_no"]

        if plate not in roster_set:
            _invalid(f"版号 {plate!r} 不在本期版号清单中", "unknown_plate")
        if plate in by_plate:
            _invalid(f"版号 {plate!r} 在交档索引中出现了多次", "duplicate_plate")
        if volume_code not in volume_map:
            _invalid(
                f"版号 {plate!r} 指定的卷 {volume_code!r} 尚未提交",
                "unknown_volume",
            )
        if frame_no not in frames_by_volume[volume_code]:
            _invalid(
                f"版号 {plate!r} 指定的原帧 {frame_no} 不是卷 {volume_code!r} 的已扫帧",
                "unknown_frame_no",
            )
        key = (volume_code, frame_no)
        if key in used_frames:
            _invalid(
                f"卷 {volume_code!r} 的原帧 {frame_no} 被多个版号重复选用",
                "duplicate_frame_no",
            )

        used_frames.add(key)
        by_plate[plate] = item

    missing = [plate for plate in roster if plate not in by_plate]
    if missing:
        _invalid(
            "交档索引缺少本期版号：" + "、".join(repr(plate) for plate in missing),
            "missing_plate",
        )

    ordered = [by_plate[plate] for plate in roster]

    picks_by_volume: dict[str, dict[int, str]] = {}
    for item in ordered:
        picks_by_volume.setdefault(item["volume_code"], {})[
            item["frame_no"]
        ] = item["plate"]

    for volume_code, picks in picks_by_volume.items():
        frames = _volume_frames(volume_map[volume_code])
        try:
            feasible = coverage.feasible_with_picks(roster, frames, picks)
        except coverage.ArrangementError as exc:
            _invalid(
                f"卷 {volume_code!r} 的交档选择无法排通：{exc.message}",
                "infeasible_picks",
            )
        if not feasible:
            _invalid(
                f"卷 {volume_code!r} 所选的原帧无法在同一套合法排法里同时拍到指定版",
                "infeasible_picks",
            )

    return ordered


def index_view(
    issue_id: str,
    entries: Sequence[Mapping[str, Any]],
    approved_at: str,
    updated_at: str,
    *,
    status: str = "approved",
    reason: str | None = None,
) -> dict[str, Any]:
    """对外的交档索引视图。

    ``status`` 为 ``approved`` 时表示正在供档；为 ``withdrawn`` 时条目仅作
    留档，必须重新核定才会恢复供档。
    """

    ordered = [
        {
            "plate": item["plate"],
            "volume_code": item["volume_code"],
            "frame_no": item["frame_no"],
        }
        for item in entries
    ]
    mapping = {
        item["plate"]: {
            "volume_code": item["volume_code"],
            "frame_no": item["frame_no"],
        }
        for item in ordered
    }
    view = {
        "issue_id": issue_id,
        "status": status,
        "serving": status == "approved",
        "entries": ordered,
        "index": mapping,
        "plate_count": len(ordered),
        "approved_at": approved_at,
        "updated_at": updated_at,
    }
    if reason is not None:
        view["reason"] = reason
    return view
