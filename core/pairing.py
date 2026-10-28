"""配对协议（M2）：一次性配对码 ↔ 每台机器的长期令牌。

流程：
  1. 已绑定身份在 QQ 私聊发「配对」→ 拿到 8 位码（TTL 600s、单次有效、限流）
  2. 新电脑上的 Coopanion 桥接设置里填这个码 → POST /bridge/pair
  3. 兑换成功 → 码当场作废，签发 32 字节机器令牌，该机器此后凭令牌访问

安全边界：
  - 配对码只在本人 QQ 会话里出现，且只能兑换一次，用完即焚；
  - 机器令牌按身份隔离，M4 的 recall 只能读到该身份的数据；
  - 身份解绑时，名下所有设备令牌一并吊销（revoke_all）。
"""

from __future__ import annotations

import secrets
import string
import time
from typing import Any, Optional

CODE_LEN = 8
CODE_TTL = 600          # 配对码有效期（秒）
MAX_ACTIVE_CODES = 3   # 同一身份同时存在的有效码上限
RATE_WINDOW = 3600     # 限流窗口（秒）
RATE_MAX = 5           # 窗口内最多申请的码数量
TOKEN_NBYTES = 32


class PairError(Exception):
    """配对流程中的可预期失败，消息可直接回给用户。"""


class PairService:
    """配对码与设备令牌的签发/吊销。依赖 store 的 raw_get/raw_put。"""

    KV_CODES = "pair_codes"
    KV_DEVICES = "devices"
    KV_RATE = "pair_issues"

    def __init__(self, store, code_ttl: int = CODE_TTL):
        self._store = store
        self.code_ttl = code_ttl

    # ---------- 配对码 ----------

    async def issue_code(self, identity_id: str) -> str:
        """签发一个新的配对码，返回码本身。超量或超限时抛 PairError。"""
        rate = await self._get(self.KV_RATE, {}) or {}
        stamps = [t for t in rate.get(identity_id, []) if time.time() - t < RATE_WINDOW]
        if len(stamps) >= RATE_MAX:
            raise PairError("申请配对码太频繁了，一小时后再试。")

        codes = await self._get(self.KV_CODES, {}) or {}
        self._purge(codes, time.time())
        active = [e for e in codes.values() if e.get("identity_id") == identity_id]
        if len(active) >= MAX_ACTIVE_CODES:
            raise PairError(
                f"你已经有 {len(active)} 个没用掉的配对码了，先用掉一个再来拿新的。"
            )

        code = self._new_code(codes)
        codes[code] = {"identity_id": identity_id, "created_at": time.time()}
        await self._put(self.KV_CODES, codes)

        stamps.append(time.time())
        rate[identity_id] = stamps[-RATE_MAX:]
        await self._put(self.KV_RATE, rate)
        return code

    async def redeem(self, code: str, device_name: str = "") -> dict:
        """用配对码换机器令牌。码当场作废。"""
        code = (code or "").strip()
        if not code:
            raise PairError("配对码不能为空。")
        codes = await self._get(self.KV_CODES, {}) or {}
        self._purge(codes, time.time())
        entry = codes.get(code)
        if entry is None:
            raise PairError("配对码不对，或者已经过期了。在 QQ 里发「配对」重新拿一个。")
        codes.pop(code)
        await self._put(self.KV_CODES, codes)

        identity_id = entry["identity_id"]
        devices = await self._get(self.KV_DEVICES, {}) or {}
        device_id = secrets.token_hex(6)
        name = (device_name or "").strip() or f"设备{len(devices) + 1}"
        device = {
            "device_id": device_id,
            "identity_id": identity_id,
            "name": name,
            "token": secrets.token_urlsafe(TOKEN_NBYTES),
            "bound_at": time.time(),
            "last_seen": None,
        }
        devices[device_id] = device
        await self._put(self.KV_DEVICES, devices)
        return device

    # ---------- 设备 ----------

    async def list_devices(self, identity_id: str) -> list:
        devices = await self._get(self.KV_DEVICES, {}) or {}
        mine = [d for d in devices.values() if d.get("identity_id") == identity_id]
        mine.sort(key=lambda d: d.get("bound_at", 0))
        return mine

    async def revoke(self, identity_id: str, selector: str) -> Optional[dict]:
        """按名字或 device_id 吊销一台设备。只能吊销自己身份名下的。"""
        selector = (selector or "").strip()
        if not selector:
            return None
        devices = await self._get(self.KV_DEVICES, {}) or {}
        target = None
        for device_id, device in devices.items():
            if device.get("identity_id") != identity_id:
                continue
            if device_id == selector or (device.get("name") or "") == selector:
                target = device_id
                break
        if target is None:
            return None
        removed = devices.pop(target)
        await self._put(self.KV_DEVICES, devices)
        return removed

    async def revoke_all(self, identity_id: str) -> int:
        """身份注销时调用：吊销名下所有设备，并作废未使用的配对码。"""
        devices = await self._get(self.KV_DEVICES, {}) or {}
        doomed = [k for k, d in devices.items() if d.get("identity_id") == identity_id]
        for k in doomed:
            devices.pop(k)
        if doomed:
            await self._put(self.KV_DEVICES, devices)

        codes = await self._get(self.KV_CODES, {}) or {}
        stale = [c for c, e in codes.items() if e.get("identity_id") == identity_id]
        for c in stale:
            codes.pop(c)
        if stale:
            await self._put(self.KV_CODES, codes)
        return len(doomed)

    async def device_by_token(self, token: str) -> Optional[dict]:
        """令牌 → 设备（M4 recall 鉴权用）。"""
        if not token:
            return None
        devices = await self._get(self.KV_DEVICES, {}) or {}
        for device in devices.values():
            if device.get("token") == token:
                return device
        return None

    async def touch(self, device_id: str) -> None:
        """更新最后活跃时间（M3 推送 / M4 检索时调用）。"""
        devices = await self._get(self.KV_DEVICES, {}) or {}
        device = devices.get(device_id)
        if device is None:
            return
        device["last_seen"] = time.time()
        await self._put(self.KV_DEVICES, devices)

    # ---------- 内部 ----------

    @staticmethod
    def _new_code(existing: dict) -> str:
        alphabet = string.digits
        for _ in range(64):
            code = "".join(secrets.choice(alphabet) for _ in range(CODE_LEN))
            if code not in existing:
                return code
        raise PairError("生成配对码失败，稍后再试。")

    @staticmethod
    def _purge(codes: dict, now: float) -> None:
        expired = [
            c for c, e in codes.items()
            if now - float(e.get("created_at", 0)) > CODE_TTL
        ]
        for c in expired:
            codes.pop(c, None)

    async def _get(self, key: str, default: Any) -> Any:
        return await self._store.raw_get(key, default)

    async def _put(self, key: str, value: Any) -> None:
        await self._store.raw_put(key, value)
