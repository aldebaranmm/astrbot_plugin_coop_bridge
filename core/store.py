"""身份注册表与状态存储。

全部走 AstrBot 插件 KV（put_kv_data/get_kv_data/delete_kv_data），
插件维度隔离，不建自己的库文件。

KV 布局：
  identities         {identity_id: {...}}          已绑定身份
  pending_binds      {identity_id: {qq, created_at}}  等待「确认」的绑定
  pending_unbinds    {identity_id: {qq, created_at}}  等待「确认解绑」的注销
  pair_codes         {code: {identity_id, created_at}}  M2 一次性配对码
  devices           {device_id: {...token...}}         M2 已配对的机器令牌
  pair_issues        {identity_id: [ts...]}             M2 配对码申请限流

identity_id 形如 "aiocqhttp:2760546027"（平台:QQ号）。
"""

from __future__ import annotations

import time
from typing import Any, Optional


def _native_id(value: Any) -> str:
    """OneBot 的 id 可能是 int 或 str；只认纯数字。"""
    if value is None:
        return ""
    text = str(value).strip()
    return text if text.isdigit() else ""


class BridgeStore:
    """KV 存取 + 绑定状态机。kv 需提供 async get(key, default) / put(key, value)。"""

    KV_IDENTITIES = "identities"
    KV_PENDING_BINDS = "pending_binds"
    KV_PENDING_UNBINDS = "pending_unbinds"

    def __init__(self, kv):
        self._kv = kv

    # ---------- 身份解析 ----------

    @staticmethod
    def resolve_identity(event: Any) -> Optional[dict]:
        """从消息事件解析身份。取不到返回 None，调用方应忽略该消息。

        三个字段都来自协议层（platform_meta / message_obj），
        不信任消息正文里任何自报的号码。
        """
        meta = getattr(event, "platform_meta", None)
        message = getattr(event, "message_obj", None)
        platform_id = getattr(meta, "id", None)
        account_id = _native_id(getattr(message, "self_id", None))
        sender = getattr(message, "sender", None)
        participant_id = _native_id(getattr(sender, "user_id", None))
        if not platform_id or not account_id or not participant_id:
            return None
        if account_id == participant_id:
            return None  # 机器人自己
        return {
            "identity_id": f"{platform_id}:{participant_id}",
            "platform_id": str(platform_id),
            "account_id": account_id,
            "participant_id": participant_id,
        }

    @staticmethod
    def identity_from_umo(umo: str) -> Optional[dict]:
        """从会话 UMO 反解身份（M3 主动消息路由用）。

        形如 "aiocqhttp:FriendMessage:2760546027"。
        """
        if not isinstance(umo, str) or not umo:
            return None
        parts = umo.split(":")
        if len(parts) != 3:
            return None
        platform_id, msg_type, participant_id = parts
        if msg_type != "FriendMessage" or not _native_id(participant_id):
            return None
        return {
            "identity_id": f"{platform_id}:{participant_id}",
            "platform_id": platform_id,
            "participant_id": participant_id,
        }

    # ---------- 已绑定身份 ----------

    async def get_identity(self, identity_id: str) -> Optional[dict]:
        identities = await self._get(self.KV_IDENTITIES, {}) or {}
        return identities.get(identity_id)

    async def commit_bind(self, identity: dict, nickname: str = "") -> dict:
        identities = await self._get(self.KV_IDENTITIES, {}) or {}
        record = {
            "platform_id": identity["platform_id"],
            "account_id": identity.get("account_id", ""),
            "participant_id": identity["participant_id"],
            "nickname": nickname or "",
            "bound_at": time.time(),
        }
        identities[identity["identity_id"]] = record
        await self._put(self.KV_IDENTITIES, identities)
        return record

    async def delete_identity(self, identity_id: str) -> bool:
        identities = await self._get(self.KV_IDENTITIES, {}) or {}
        if identity_id not in identities:
            return False
        identities.pop(identity_id)
        await self._put(self.KV_IDENTITIES, identities)
        return True

    # ---------- 待确认绑定 ----------

    async def put_pending_bind(self, identity: dict) -> None:
        pending = await self._get(self.KV_PENDING_BINDS, {}) or {}
        pending[identity["identity_id"]] = {
            "qq": identity["participant_id"],
            "created_at": time.time(),
        }
        await self._put(self.KV_PENDING_BINDS, pending)

    async def get_pending_bind(self, identity_id: str, ttl: int) -> Optional[dict]:
        pending = await self._get(self.KV_PENDING_BINDS, {}) or {}
        entry = pending.get(identity_id)
        if not entry:
            return None
        if self._expired(entry, ttl):
            pending.pop(identity_id, None)
            await self._put(self.KV_PENDING_BINDS, pending)
            return None
        return entry

    async def clear_pending_bind(self, identity_id: str) -> None:
        pending = await self._get(self.KV_PENDING_BINDS, {}) or {}
        if identity_id in pending:
            pending.pop(identity_id)
            await self._put(self.KV_PENDING_BINDS, pending)

    # ---------- 待确认解绑 ----------

    async def put_pending_unbind(self, identity_id: str) -> None:
        pending = await self._get(self.KV_PENDING_UNBINDS, {}) or {}
        pending[identity_id] = {"created_at": time.time()}
        await self._put(self.KV_PENDING_UNBINDS, pending)

    async def get_pending_unbind(self, identity_id: str, ttl: int) -> Optional[dict]:
        pending = await self._get(self.KV_PENDING_UNBINDS, {}) or {}
        entry = pending.get(identity_id)
        if not entry:
            return None
        if self._expired(entry, ttl):
            pending.pop(identity_id, None)
            await self._put(self.KV_PENDING_UNBINDS, pending)
            return None
        return entry

    async def clear_pending_unbind(self, identity_id: str) -> None:
        pending = await self._get(self.KV_PENDING_UNBINDS, {}) or {}
        if identity_id in pending:
            pending.pop(identity_id)
            await self._put(self.KV_PENDING_UNBINDS, pending)

    # ---------- 内部 ----------

    @staticmethod
    def _expired(entry: dict, ttl: int) -> bool:
        try:
            return (time.time() - float(entry.get("created_at", 0))) > ttl
        except (TypeError, ValueError):
            return True

    async def _get(self, key: str, default: Any) -> Any:
        return await self._kv.get_kv_data(key, default)

    async def _put(self, key: str, value: Any) -> None:
        await self._kv.put_kv_data(key, value)

    # ---------- 供 PairService 使用的通用读写 ----------

    async def raw_get(self, key: str, default: Any = None) -> Any:
        return await self._get(key, default)

    async def raw_put(self, key: str, value: Any) -> None:
        await self._put(key, value)
