"""游戏期间攒下的话（M7）。

行为：叫不应想搞事、但检测到游戏在跑时，先把这条话存起来；游戏关掉后由补发任务取走。
按会话（umo）去重——同一个会话只留最新一条，避免连叫几次攒成一堆旧话。
超过 TTL 自动丢弃，免得半夜攒的话第二天早上突然冒出来。
"""

from __future__ import annotations

import time
from typing import Any, Optional

# 攒着的话最多留多久（秒）：默认半小时，超过就丢
DEFAULT_TTL = 1800


def next_unanswered(current: int) -> int:
    """本次发送「之后」的未回复次数。

    挂钩点在主动消息的发送出口，此时主动消息插件还没把这一条计入
    （它在发送返回之后才 `unanswered_count += 1`），所以要 +1 才是真实值。
    """
    try:
        value = int(current)
    except (TypeError, ValueError):
        value = 0
    return max(0, value) + 1


class DeferredStunts:
    def __init__(self, ttl: float = DEFAULT_TTL):
        self.ttl = float(ttl)
        self._items: dict[str, dict[str, Any]] = {}

    def put(self, umo: str, text: str, meta: Optional[dict] = None) -> None:
        """存下一条待补发的话；同会话覆盖旧的那条。"""
        if not umo or not (text or "").strip():
            return
        self._items[umo] = {
            "text": (text or "").strip(),
            "meta": dict(meta or {}),
            "ts": time.time(),
        }

    def pending(self) -> dict[str, dict[str, Any]]:
        self.expire()
        return dict(self._items)

    def take(self, umo: str) -> Optional[dict[str, Any]]:
        """取走一条（取走即删除）。"""
        return self._items.pop(umo, None)

    def clear(self) -> None:
        self._items.clear()

    def expire(self, now: Optional[float] = None) -> int:
        """丢掉过期项，返回丢弃条数。"""
        current = time.time() if now is None else now
        expired = [k for k, v in self._items.items() if current - float(v.get("ts", 0)) > self.ttl]
        for key in expired:
            self._items.pop(key, None)
        return len(expired)

    def __len__(self) -> int:
        return len(self._items)
