"""把一段时间的电脑活动压成一句，写进 livingmemory（M8 补充）。

用户要的是「定时汇总一条」：
不要每 60 秒记一条流水账（那会把记忆图谱冲成日志），
而是每半小时把「在干什么、切了几次程序、有没有离开」压成一句人话。

设计要点：
- 只处理上一次汇总点之后的活动，不重复写。
- 样本太少（刚启动、刚上报）先不动标记，等下次一起算。
- 整段大半时间不在电脑前时也会说明，方便以后回忆作息。
"""

from __future__ import annotations

import time
from statistics import median
from typing import Any, Optional

# 汇总间隔的允许范围（分钟）
MIN_MINUTES = 10
MAX_MINUTES = 180
DEFAULT_MINUTES = 30

# 一条汇总至少要这么多采样，否则样本太少不写
MIN_ENTRIES = 3

# 单次汇总覆盖的最长时间：超出就只取最近这段，
# 避免一次写下「6 小时都在同一件事」（跨度太长也没参考价值）
MAX_SPAN_SECONDS = 3 * 3600

# 相邻采样缺时间戳时，用它估算单条停留时长（秒）
FALLBACK_GAP = 60.0

# 启动后第一次汇总的等待时间：别让人等满一个周期才看到第一条记忆
BOOTSTRAP_SECONDS = 180

# 单次采样空闲超过这个秒数，就当这一次「人不在电脑前」
AWAY_IDLE_SECONDS = 300

# 主程序占比达到这个比例，才说「一直在用它」
DOMINANT_RATIO = 0.6

# 窗口标题最多留多少字
TITLE_LIMIT = 60


def identity_to_umo(identity_id: str) -> str:
    """身份 `platform:participant` → 会话 UMO `platform:FriendMessage:participant`。

    **这是隔离的关键**：汇总写记忆时必须用「这个身份自己的」会话，
    不能借用别人的 umo，否则记忆就串了。
    """
    parts = (identity_id or "").split(":")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return ""
    return "{}:FriendMessage:{}".format(parts[0], parts[1])


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ts(entry: Any) -> float:
    if not isinstance(entry, dict):
        return 0.0
    return _number(entry.get("ts"))


def _clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _minutes(seconds: float) -> int:
    return max(1, int(round(seconds / 60.0)))


def _normalize(entries: list) -> list[dict]:
    """把原始活动条目洗成聚合需要的最小字段，丢掉坏数据。"""
    cleaned: list[dict] = []
    for entry in entries or []:
        ts = _ts(entry)
        if ts <= 0:
            continue
        window = entry.get("window") if isinstance(entry.get("window"), dict) else {}
        cleaned.append(
            {
                "ts": ts,
                "process": str(window.get("process") or "").strip(),
                "title": str(window.get("title") or "").strip(),
                "idle": max(0.0, _number(entry.get("idle_seconds"))),
            }
        )
    cleaned.sort(key=lambda item: item["ts"])
    return cleaned


def collapse(entries: list) -> Optional[str]:
    """把一批活动压成一句话；样本不足或没有信息量时返回 None。"""
    items = _normalize(entries)
    if len(items) < MIN_ENTRIES:
        return None
    start, end = items[0]["ts"], items[-1]["ts"]
    if end - start < 60:
        return None

    # 停留时长：用相邻采样的间隔近似；最后一条按中位间隔估
    gaps = [min(max(b["ts"] - a["ts"], 1.0), 600.0) for a, b in zip(items, items[1:])]
    step = median(gaps) if gaps else FALLBACK_GAP
    weights = gaps + [step]

    stays: dict[str, float] = {}
    titles: dict[str, str] = {}
    switches = 0
    away = 0
    previous: Optional[str] = None
    for item, weight in zip(items, weights):
        name = item["process"] or "(未知程序)"
        stays[name] = stays.get(name, 0.0) + weight
        if name not in titles and item["title"]:
            titles[name] = item["title"]
        if previous is not None and name != previous:
            switches += 1
        previous = name
        if item["idle"] >= AWAY_IDLE_SECONDS:
            away += 1

    total = sum(stays.values())
    if total <= 0:
        return None
    ranked = sorted(stays.items(), key=lambda kv: -kv[1])
    top_name, top_seconds = ranked[0]
    share = top_seconds / total

    span = f"{_clock(start)}–{_clock(end)}"
    if share >= DOMINANT_RATIO:
        line = f"【电脑活动】{span} 主人一直在用 {top_name}（约 {_minutes(total)} 分钟）"
        title = titles.get(top_name, "")
        # 标题和程序名一模一样（比如 WorkBuddy）就别重复说了
        if title and title.lower() != top_name.lower():
            line += f"，窗口是「{title[:TITLE_LIMIT]}」"
    else:
        others = [name for name, _ in ranked[1:4]]
        line = f"【电脑活动】{span} 主人主要在 {top_name}（约 {_minutes(top_seconds)} 分钟）"
        if len(others) == 1:
            line += f"，期间切到 {others[0]} {switches} 次"
        else:
            line += f"，期间在 {'、'.join(others)} 之间切换了 {switches} 次"

    away_ratio = away / len(items)
    if away_ratio >= 0.5:
        average_idle = sum(item["idle"] for item in items) / len(items)
        line += f"；其中大半时间没在电脑前（平均空闲约 {_minutes(average_idle)} 分钟）"

    return line


class ActivityDigest:
    """按身份记录「上次汇总到哪」，到点产出一句可写进记忆的话。"""

    def __init__(self, minutes: int = DEFAULT_MINUTES, bootstrap_seconds: int = BOOTSTRAP_SECONDS):
        self.minutes = max(MIN_MINUTES, min(MAX_MINUTES, int(_number(minutes, DEFAULT_MINUTES) or DEFAULT_MINUTES)))
        # 启动后先快速沉淀一条，之后才按 minutes 的节奏走
        self.bootstrap_seconds = max(60, int(_number(bootstrap_seconds, BOOTSTRAP_SECONDS)))
        self._marks: dict[str, float] = {}
        self._written: set[str] = set()

    @property
    def interval_seconds(self) -> int:
        return self.minutes * 60

    def due(self, identity_id: str, now: Optional[float] = None) -> bool:
        """是否到汇总时间。第一次见到这个身份时只记下起点，不立刻写。"""
        if not identity_id:
            return False
        moment = time.time() if now is None else now
        mark = self._marks.get(identity_id)
        if mark is None:
            self._marks[identity_id] = moment
            return False
        wait = self.interval_seconds if identity_id in self._written else self.bootstrap_seconds
        return (moment - mark) >= wait

    def take(self, identity_id: str, history: list, now: Optional[float] = None) -> Optional[str]:
        """取出标记点之后的活动并压成一句；样本不足时不动标记。"""
        if not identity_id:
            return None
        moment = time.time() if now is None else now
        mark = self._marks.get(identity_id)
        if mark is None:
            return None
        cutoff = max(mark, moment - MAX_SPAN_SECONDS)
        window = [entry for entry in (history or []) if _ts(entry) > cutoff]
        if len(window) < MIN_ENTRIES:
            # 样本太少：保留标记，下次把这段一起算进去
            return None
        text = collapse(window)
        if text is None:
            # 有数据但没压缩出结果（比如跨度太短）：推掉这段，别反复重算
            self._marks[identity_id] = moment
            return None
        self._marks[identity_id] = moment
        self._written.add(identity_id)
        return text

    def forget(self, identity_id: str) -> None:
        self._marks.pop(identity_id, None)
        self._written.discard(identity_id)
