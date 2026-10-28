"""M3：主动消息双发（QQ + Coopanion）。

思路：主动消息插件（astrbot_plugin_proactive_chat）负责生成并发送到 QQ，
我们不去改它的任何一行源码，而是在它真正落地的那个函数外面套一层：

    SenderMixin._send_proactive_message(session_id, text, event, initial_chain)
        → 原样调用，返回值与异常语义完全不变
        → 调用结束后把这次的文本抄送一份到 outbox（/bridge/inbox 拉取）

为什么选这个函数：
  - 它是主动消息的唯一出口（chat_flow 生成完文本后只调它一次）；
  - 参数里有干净的 text 和会话 UMO，不用去解析消息链；
  - 返回值 True/False 明确表示「有没有真的发到 QQ」，可以写进 meta。

为什么是类级包装（patch class method）而不是改文件：
  - 第三方插件升级/重装后不会被覆盖，也不需要用户手动打补丁；
  - 它没装、没启用、或改名了，只是跳过双发，不影响本插件其它能力；
  - 补丁被卸载（terminate）时会还原原函数，不留残钩。
"""

from __future__ import annotations

import functools
import importlib
import inspect
from typing import Any, Callable, Optional

TARGET_MODULE = "data.plugins.astrbot_plugin_proactive_chat.core.message_sender"
TARGET_CLASS = "SenderMixin"
TARGET_METHOD = "_send_proactive_message"
PATCH_FLAG = "_coop_bridge_patched"

_state: dict = {
    "handler": None,    # Callable[[umo, text, dict], Any]，可为协程函数
    "target": None,      # 被补丁的类
    "orig": None,        # 原函数
    "logger": None,
}


def _log(level: str, message: str, *args) -> None:
    logger = _state.get("logger")
    if logger is None:
        return
    try:
        getattr(logger, level)(message, *args)
    except Exception:  # pragma: no cover - 日志不能反噬业务
        pass


def _target_class() -> Optional[type]:
    """拿到 SenderMixin 类。插件没装/没启用时返回 None。"""
    try:
        module = importlib.import_module(TARGET_MODULE)
    except Exception as exc:  # 模块不存在或导入失败 = 没装这个插件
        _log("info", "[coop_bridge] 未找到主动消息插件，跳过双发挂钩: %s", exc)
        return None
    cls = getattr(module, TARGET_CLASS, None)
    if cls is None:
        _log("warning", "[coop_bridge] 主动消息插件里没有 %s，跳过双发挂钩", TARGET_CLASS)
        return None
    return cls


def install(handler: Callable[..., Any], logger: Any = None) -> bool:
    """把 handler 挂到主动消息发送链路上。重复调用是安全的。"""
    _state["handler"] = handler
    _state["logger"] = logger

    cls = _target_class()
    if cls is None:
        return False
    if getattr(cls, PATCH_FLAG, False):
        return True  # 已挂过（handler 已更新为最新引用）

    orig = getattr(cls, TARGET_METHOD, None)
    if orig is None:
        _log("warning", "[coop_bridge] 主动消息插件缺少 %s，跳过双发挂钩", TARGET_METHOD)
        return False

    @functools.wraps(orig)
    async def wrapper(self, *args, **kwargs):
        result = await orig(self, *args, **kwargs)
        handler_ref = _state.get("handler")
        if handler_ref is not None:
            try:
                umo = kwargs.get("session_id") or (args[0] if args else "")
                text = kwargs.get("text") or (args[1] if len(args) > 1 else "")
                outcome = handler_ref(umo, text or "", {"delivered": result is True})
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception as exc:
                _log(
                    "warning",
                    "[coop_bridge] 主动消息抄送 Coopanion 失败（不影响 QQ 已发出的消息）: %s",
                    exc,
                )
        return result

    wrapper.__coop_bridge_orig__ = orig
    setattr(cls, TARGET_METHOD, wrapper)
    setattr(cls, PATCH_FLAG, True)
    _state["target"] = cls
    _state["orig"] = orig
    _log("info", "[coop_bridge] 已挂钩主动消息双发（%s.%s）", TARGET_CLASS, TARGET_METHOD)
    return True


def uninstall() -> bool:
    """还原原函数。插件卸载时必须调用，避免留下指向已销毁对象的钩子。"""
    cls = _state.get("target")
    orig = _state.get("orig")
    _state["handler"] = None
    if cls is None or orig is None:
        return False
    current = getattr(cls, TARGET_METHOD, None)
    if getattr(current, "__coop_bridge_orig__", None) is not orig:
        return False
    try:
        setattr(cls, TARGET_METHOD, orig)
        if hasattr(cls, PATCH_FLAG):
            try:
                delattr(cls, PATCH_FLAG)
            except AttributeError:
                pass
    except Exception as exc:  # pragma: no cover
        _log("warning", "[coop_bridge] 还原主动消息发送函数失败: %s", exc)
        return False
    _state["target"] = None
    _state["orig"] = None
    return True


def is_installed() -> bool:
    cls = _state.get("target")
    return bool(cls is not None and getattr(cls, PATCH_FLAG, False))


async def route_to_outbox(store: Any, push: Callable[..., Any], umo: str, text: str, meta: Optional[dict] = None) -> bool:
    """M3 路由：会话 UMO → 身份 → 该身份的 outbox。返回是否真的投递。

    两条硬规则：
      - 只认私聊（identity_from_umo 只接受 FriendMessage），群聊主动消息不外流；
      - 只推给已在插件里绑定过的身份，陌生人不进任何队列。
    """
    identity = store.identity_from_umo(umo)
    if identity is None:
        return False
    identity_id = identity["identity_id"]
    record = await store.get_identity(identity_id)
    if record is None:
        return False
    body = (text or "").strip()
    if not body:
        return False  # 纯图片/语音没法用文本抄送，等 M5 再处理附件
    push(
        identity_id,
        body,
        {
            "kind": "proactive",
            "delivered": bool((meta or {}).get("delivered")),
            "umo": umo,
            # M7：连叫几次没应声时，附带一个「搞事」动作，由桌宠执行
            **{
                key: (meta or {})[key]
                for key in ("stunt", "unanswered")
                if (meta or {}).get(key) is not None
            },
        },
    )
    return True
