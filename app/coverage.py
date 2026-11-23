"""排法可达性计算。

核心约束
--------
同一卷内，相邻两帧拍同一版或下一版（版序后一位），因此把每一帧的版序
看作路径上的位置时：

* 版序单调不减；
* 相邻帧的版序最多前进 1；
* 已扫帧的版序必须落在该帧交来的候选版号里，候选为空的帧不受限。

排法数量随帧数指数增长（最多 500 帧），因此这里不做枚举，而是用位图
前向/后向可达性 DP 求出：

* 某卷是否可排通；
* 每个版是否可能由某个已扫帧拍到（∃ 排法）；
* 每个版是否在所有排法中都有已扫帧拍到（∀ 排法）；
* 每个版是否可能由某个漏扫帧拍到（用于缺扫时给出线索）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

MAX_FRAMES_PER_VOLUME = 500

STATUS_SCANNED = "scanned"
STATUS_PENDING = "pending"
STATUS_MISSING = "missing"

STATUS_LABELS = {
    STATUS_SCANNED: "已扫",
    STATUS_PENDING: "待核",
    STATUS_MISSING: "缺扫",
}


class ArrangementError(ValueError):
    """卷内数据无法构成任何排法（或数据不合法）。"""

    def __init__(self, message: str, code: str = "infeasible") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class Frame:
    frame_no: int
    candidates: tuple[str, ...]  # 空元组表示“认不出”，即不限版


def normalize_roster(raw: Iterable[Any]) -> list[str]:
    """校验全期版号清单，保持提交的版序。"""

    roster: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            raise ArrangementError(f"版号必须是字符串，收到 {value!r}", "invalid_plate")
        label = value.strip()
        if not label:
            raise ArrangementError("版号不能为空", "invalid_plate")
        if label in roster:
            raise ArrangementError(f"本期版号清单存在重复版号 {label!r}", "duplicate_plate")
        roster.append(label)
    if not roster:
        raise ArrangementError("本期版号清单不能为空", "empty_roster")
    return roster


def normalize_frames(
    raw_frames: Iterable[dict[str, Any]],
    roster: Sequence[str],
    *,
    max_frames: int = MAX_FRAMES_PER_VOLUME,
) -> list[Frame]:
    """校验并规范化一卷的已扫帧。

    帧号必须为正整数且不重复；候选版号必须在本期清单内；
    首末已扫帧之间（含已扫帧）最多 ``max_frames`` 帧。
    """

    plate_set = set(roster)
    frames: list[Frame] = []
    seen: set[int] = set()

    for raw in raw_frames:
        frame_no = raw["frame_no"]
        if not isinstance(frame_no, int) or isinstance(frame_no, bool) or frame_no < 1:
            raise ArrangementError(f"原帧号必须为正整数，收到 {frame_no!r}", "invalid_frame_no")
        if frame_no in seen:
            raise ArrangementError(f"原帧号 {frame_no} 重复提交", "duplicate_frame_no")
        seen.add(frame_no)

        candidates: list[str] = []
        for value in raw.get("candidates") or ():
            label = str(value).strip()
            if not label:
                continue
            if label not in plate_set:
                raise ArrangementError(
                    f"原帧 {frame_no} 的候选版号 {label!r} 不在本期版号清单中",
                    "unknown_plate",
                )
            if label not in candidates:
                candidates.append(label)
        frames.append(Frame(frame_no=frame_no, candidates=tuple(candidates)))

    if not frames:
        raise ArrangementError("一卷至少需要一个已扫帧", "empty_volume")

    frames.sort(key=lambda frame: frame.frame_no)
    span = frames[-1].frame_no - frames[0].frame_no + 1
    if span > max_frames:
        raise ArrangementError(
            f"首末已扫帧之间共 {span} 帧，超过每卷 {max_frames} 帧的上限",
            "span_too_large",
        )
    return frames


def _popcount_bits(bits: int) -> Iterable[int]:
    """逐个产出位图中置位的 1-based 版序。"""
    while bits:
        low = bits & -bits
        yield low.bit_length()
        bits ^= low


def _prefix_reachable(allowed: list[int], all_mask: int) -> list[int] | None:
    """前向可达：每帧可能落在哪些版序上（前缀约束）。"""
    reachable: list[int] = []
    current = allowed[0]
    if not current:
        return None
    reachable.append(current)
    for mask in allowed[1:]:
        current = ((current | (current << 1)) & all_mask) & mask
        if not current:
            return None
        reachable.append(current)
    return reachable


def _suffix_reachable(allowed: list[int], all_mask: int) -> list[int] | None:
    """后向可达：每帧必须落在哪些版序上才能接到末尾（后缀约束）。"""
    reachable: list[int] | None = [0] * len(allowed)
    current = allowed[-1]
    if not current:
        return None
    reachable[-1] = current
    for index in range(len(allowed) - 2, -1, -1):
        current = ((current | (current >> 1)) & all_mask) & allowed[index]
        if not current:
            return None
        reachable[index] = current
    return reachable


def _is_feasible_without_page(
    allowed: list[int],
    scanned_positions: frozenset[int],
    page_bit: int,
    all_mask: int,
) -> bool:
    """在“已扫帧一律不落在 page_bit 这一版”的前提下是否仍可排通。

    漏扫帧不受该限制（它们本来就没有候选版号）。
    """
    current = 0
    for index, mask in enumerate(allowed):
        if index in scanned_positions:
            mask &= ~page_bit
        if index == 0:
            current = mask
        else:
            current = ((current | (current << 1)) & all_mask) & mask
        if not current:
            return False
    return True


def compute_volume(
    roster: Sequence[str],
    frames: Sequence[Frame],
    *,
    max_frames: int = MAX_FRAMES_PER_VOLUME,
) -> dict[str, Any]:
    """计算单卷结果。

    返回::

        {
          "feasible": True,
          "span": {...},
          "pages": {
             "<版号>": {
                "possible_scanned_frames": [...],
                "possible_missed_frames": [...],
                "guaranteed": bool,
             }, ...
          }
        }

    任何不合法/排不通的情况都会抛出 :class:`ArrangementError`。
    """

    roster = list(roster)
    if not roster:
        raise ArrangementError("本期版号清单为空", "empty_roster")
    if len(set(roster)) != len(roster):
        raise ArrangementError("本期版号清单存在重复版号", "duplicate_plate")
    frames = list(frames)
    if not frames:
        raise ArrangementError("一卷至少需要一个已扫帧", "empty_volume")
    if len(frames) > max_frames:
        raise ArrangementError(
            f"一卷已扫帧 {len(frames)} 个，超过每卷 {max_frames} 帧的上限",
            "too_many_frames",
        )
    frames.sort(key=lambda frame: frame.frame_no)

    plate_index = {plate: index for index, plate in enumerate(roster, start=1)}
    page_count = len(roster)
    all_mask = (1 << page_count) - 1

    first = frames[0].frame_no
    last = frames[-1].frame_no
    span = last - first + 1
    if span > max_frames:
        raise ArrangementError(
            f"首末已扫帧之间共 {span} 帧，超过每卷 {max_frames} 帧的上限",
            "span_too_large",
        )

    scanned_by_pos = {frame.frame_no - first: frame for frame in frames}

    allowed: list[int] = []
    for pos in range(span):
        frame = scanned_by_pos.get(pos)
        if frame is None:
            allowed.append(all_mask)
            continue
        if not frame.candidates:
            allowed.append(all_mask)
            continue
        mask = 0
        for label in frame.candidates:
            if label not in plate_index:
                raise ArrangementError(
                    f"原帧 {frame.frame_no} 的候选版号 {label!r} 不在本期版号清单中",
                    "unknown_plate",
                )
            mask |= 1 << (plate_index[label] - 1)
        if not mask:  # 正常流程不会走到这里，防御性处理
            raise ArrangementError(
                f"原帧 {frame.frame_no} 没有有效候选版号", "no_candidate"
            )
        allowed.append(mask)

    forward = _prefix_reachable(allowed, all_mask)
    if forward is None:
        raise ArrangementError("按相邻帧规则和候选版号无法排通该卷", "infeasible")
    backward = _suffix_reachable(allowed, all_mask)
    if backward is None:  # 理论上与前向同时成败
        raise ArrangementError("按相邻帧规则和候选版号无法排通该卷", "infeasible")

    possible_scanned: list[list[int]] = [[] for _ in range(page_count)]
    possible_missed: list[list[int]] = [[] for _ in range(page_count)]

    for pos in range(span):
        bits = forward[pos] & backward[pos]
        if not bits:
            # 有完整排法存在时不应发生；保守视为排不通，避免给出错误结论。
            raise ArrangementError("卷内存在无法接入任何完整排法的帧", "infeasible")
        frame_no = first + pos
        if pos in scanned_by_pos:
            for page in _popcount_bits(bits):
                possible_scanned[page - 1].append(frame_no)
        else:
            for page in _popcount_bits(bits):
                possible_missed[page - 1].append(frame_no)

    # 只有在“所有排法都必须在某个已扫帧拍到该版”时才是已扫。
    # 判定方法：禁止全部已扫帧落在该版后重新做一次可行性检查。
    scanned_positions = frozenset(scanned_by_pos)
    scanned_reachable_pages = 0
    for pos in scanned_positions:
        scanned_reachable_pages |= forward[pos] & backward[pos]

    guaranteed_pages: set[int] = set()
    for page in _popcount_bits(scanned_reachable_pages):
        page_bit = 1 << (page - 1)
        if not _is_feasible_without_page(allowed, scanned_positions, page_bit, all_mask):
            guaranteed_pages.add(page)

    pages: dict[str, Any] = {}
    for index, plate in enumerate(roster, start=1):
        pages[plate] = {
            "possible_scanned_frames": possible_scanned[index - 1],
            "possible_missed_frames": possible_missed[index - 1],
            "guaranteed": index in guaranteed_pages,
        }

    return {
        "feasible": True,
        "span": {
            "first_frame": first,
            "last_frame": last,
            "frame_count": span,
            "scanned_count": len(frames),
            "missed_count": span - len(frames),
        },
        "pages": pages,
    }
