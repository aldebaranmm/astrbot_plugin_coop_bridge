"""astrbot_plugin_coop_bridge —— AstrBot × Coopanion 桥接插件。

分期路线：
  M1 多身份绑定：私聊首次接触 → 协议层取 QQ 号 → 一次性确认 → KV 钉住
  M2 配对协议：QQ 侧配对码 ↔ 每台 Coopanion 机器令牌
  M3 主动消息双发：挂钩 proactive_chat 发送出口，QQ 照发 + Coopanion 抄送
  M4 数据出口：HTTP recall API + 记忆适配（livingmemory / angel / self_learning）

设计要点：身份只信协议层（platform_meta / message_obj.sender.user_id），
消息正文里自报的号码一律不作为身份依据。
"""

from __future__ import annotations

import asyncio
import random
import time

import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.filter.event_message_type import EventMessageType

from .core.config import BridgeConfig
from .core.activity import ActivityStore
from .core.activity_digest import ActivityDigest, identity_to_umo, AWAY_IDLE_SECONDS
from .core.memory import MemoryHub
from .core import game_watch
from .core.memory_mirror import mirror_chat, mirror_note
from .core.stunt_defer import DeferredStunts, next_unanswered
from .core.pairing import PairError, PairService
from .core.proactive import (
    install as install_proactive_hook,
    route_to_outbox,
    uninstall as uninstall_proactive_hook,
)
from .core.server import BridgeServer
from .core.store import BridgeStore

CONFIRM_WORD = "确认"
REJECT_WORD = "不是"
UNBIND_WORD = "解绑"
UNBIND_CONFIRM_WORD = "确认解绑"
PAIR_WORD = "配对"
DEVICES_WORD = "我的设备"
REVOKE_WORD = "取消配对"
VERSION = "0.9.2"

def _strip_clock(text: str) -> str:
    """去掉 HH:MM 这样的时间片段，好比较两段活动文案是不是一回事。"""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if i + 5 <= n and text[i : i + 2].isdigit() and text[i + 2] == ":" and text[i + 3 : i + 5].isdigit():
            i += 5
            continue
        out.append(text[i])
        i += 1
    return "".join(out).strip()


def _same_activity(prev: str, cur: str) -> bool:
    """两段活动汇总是不是实质重复。

    相邻时段的窗口往往一模一样（16:21–16:32 和 16:22–16:33 都是「在文件管理器」），
    重复写只会把记忆库撑大、把真正有用的记忆挤掉。
    """
    a, b = _strip_clock(prev or ""), _strip_clock(cur or "")
    if not a or not b:
        return False
    if a == b:
        return True
    set_a, set_b = set(a), set(b)
    shared = len(set_a & set_b)
    return shared / max(1, min(len(set_a), len(set_b))) >= 0.9


def _mask(native: str) -> str:
    """QQ 号打码成 276*****27，日志里不留全号。"""
    value = (native or "").strip()
    if len(value) <= 5:
        return value or "?"
    return f"{value[:3]}*****{value[-2:]}"


# M7：连着叫几次没应声时，桌宠可以做的动作。
# 只保留「走到鼠标旁边」——它只移动自己的窗口，不注入键鼠，零风险也不会被它自己否掉。
STUNT_ACTIONS = ("go_cursor",)

# 没回复就反复走过来：间隔、最长持续时间、只走路不说话的动作名
NAG_INTERVAL_SECONDS = 12
NAG_MAX_SECONDS = 15 * 60
NAG_ACTION = "walk_only"


def _noop_hook():
    """AstrBot 版本不带 on_astrbot_loaded 时的兜底装饰器（只是不挂钩）。"""

    def deco(fn):
        return fn

    return deco


def _llm_request_hook():
    """on_llm_request 不是每个版本都有；缺了退化成空装饰器，绝不让插件加载失败。"""
    factory = getattr(filter, "on_llm_request", None)
    if factory is None:
        def deco(fn):
            return fn

        return deco
    return factory()


# 加载完成钩子不是每个版本都有，缺了也不能让整个插件 import 失败
on_astrbot_loaded = getattr(filter, "on_astrbot_loaded", None) or _noop_hook


class CoopBridgePlugin(star.Star):
    def __init__(self, context: star.Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = BridgeConfig(config)
        self.store = BridgeStore(self)  # Star 自带 KV（get/put_kv_data）
        self.pair = PairService(self.store)
        data_dir = MemoryHub.resolve_data_dir(self.cfg.astrbot_data_dir)
        self.memory = MemoryHub(
            data_dir=data_dir,
            enable_angel=self.cfg.src_angel_memory,
            enable_self=self.cfg.src_self_learning,
            enable_living=self.cfg.src_livingmemory,
            include_group=self.cfg.include_group_memory,
            logger=logger,
        )
        plugin_dir = data_dir / "plugin_data" / "astrbot_plugin_coop_bridge"
        self.activity = ActivityStore(path=plugin_dir / "activity.json")
        # M8：定时把活动压成一句写进记忆（默认 30 分钟一次）
        self._digest = ActivityDigest(minutes=self.cfg.activity_digest_minutes)
        # 每个身份最近一次写进记忆的活动汇总，用来挡掉实质重复的那一条
        self._last_activity_text: dict[str, str] = {}
        self.server = BridgeServer(
            cfg=self.cfg,
            store=self.store,
            pair=self.pair,
            memory=self.memory,
            logger=logger,
            version=VERSION,
            on_chat=self._record_chat,
            activity=self.activity,
            on_video=self._record_video,
        )
        self._register_pair_api()
        self._proactive_attached = False
        # M7：游戏期间攒下的话，等游戏关掉再补发
        self._deferred = DeferredStunts()
        # 正在被"反复催"的会话：{umo: {"started": ts, "last": ts}}
        self._nagging: dict[str, dict] = {}
        self._running = False

    async def initialize(self):
        """加载、重载插件时触发。"""
        started = await self.server.start()
        self._running = True
        asyncio.create_task(self._watch_deferred())
        self._attach_proactive()
        if not self._proactive_attached:
            # 主动消息插件可能排在本插件之后才加载，给它一点时间再挂一次
            asyncio.create_task(self._retry_attach_proactive())
        logger.info(
            "[coop_bridge] 已加载（M1 绑定 + M2 配对 + M3 双发 + M4 检索 + M6 对话回传 + M7 叫不应就搞事 + M8 活动感知/汇总 + M13 远程推送）· "
            "桥接服务 %s · 主动消息双发 %s",
            "已启动" if started else "未启动（见上方告警）",
            "已挂钩" if self._proactive_attached else "未挂钩（主动消息插件未就绪）",
        )

    @on_astrbot_loaded()
    async def _hook_astrbot_loaded(self):
        """所有插件都加载完后再确认一次挂钩，覆盖「本插件先加载」的情况。"""
        if not self._proactive_attached:
            self._attach_proactive()

    def _attach_proactive(self) -> bool:
        """把 M3 抄送钩子挂到主动消息插件上。失败只告警，不影响本体。"""
        if not self.cfg.proactive_dual_send:
            return False
        try:
            ok = install_proactive_hook(self._on_proactive_message, logger)
        except Exception as exc:
            logger.warning("[coop_bridge] 挂钩主动消息失败: %s", exc)
            return False
        self._proactive_attached = bool(ok)
        return self._proactive_attached

    async def _retry_attach_proactive(self, attempts: int = 12, gap: float = 5.0):
        """加载时序兜底：最多补挂约 1 分钟。"""
        for _ in range(attempts):
            await asyncio.sleep(gap)
            if not self.cfg.proactive_dual_send:
                return
            if self._attach_proactive():
                logger.info("[coop_bridge] 主动消息双发已补挂成功")
                return

    async def _on_proactive_message(self, umo: str, text: str, meta: dict):
        """M3 回调：主动消息发往 QQ 之后，抄送一份给该身份的 Coopanion。

        只认私聊会话，且只推给已经绑定过的身份：群聊和陌生人不进 Coopanion。
        M7：连着没回达到阈值时，额外带一个「搞事」动作给桌宠。
        """
        payload = dict(meta or {})
        if self.cfg.stunt_enabled:
            # 挂钩点在发送出口，主动插件还没把这一条计入，所以按「之后」的计数算
            count = next_unanswered(self._unanswered_count(umo))
            if count >= self.cfg.stunt_after:
                payload["unanswered"] = count
                payload["stunt"] = random.choice(STUNT_ACTIONS)
                # 游戏在跑就先不动手，攒到游戏关掉再补发（避免在游戏里被弹窗/被注入打扰）
                if self.cfg.defer_in_game and await self._game_running():
                    self._deferred.put(umo, text, payload)
                    logger.info(
                        "[coop_bridge] 检测到游戏在跑，先攒着这条（%d 字），关掉后再补发",
                        len((text or "").strip()),
                    )
                    return
                logger.info(
                    "[coop_bridge] 已连续 %d 次未回复，附加搞事动作: %s",
                    count,
                    payload["stunt"],
                )
                self._start_nag(umo)
        try:
            sent = await route_to_outbox(self.store, self.server.push, umo, text, payload)
        except Exception as exc:
            logger.warning("[coop_bridge] 主动消息抄送异常（QQ 侧不受影响）: %s", exc)
            return
        if sent:
            logger.info(
                "[coop_bridge] 主动消息已抄送 Coopanion: %s（%d 字，QQ 送达=%s）",
                (umo or "").split(":")[-1],
                len((text or "").strip()),
                bool((meta or {}).get("delivered")),
            )

    def _unanswered_count(self, umo: str) -> int:
        """读主动消息插件的未回复计数；拿不到就按 0 处理（宁可不搞事）。"""
        try:
            meta = self.context.get_registered_star("astrbot_plugin_proactive_chat")
            plugin = getattr(meta, "star_cls", None) if meta is not None else None
            session_data = getattr(plugin, "session_data", None) or {}
            row = session_data.get(umo) or {}
            return int(row.get("unanswered_count", 0) or 0)
        except Exception:
            return 0

    async def _game_running(self) -> bool:
        """有没有 Steam 游戏在跑；检测失败一律当没有（宁可打扰，也不憋着不说）。"""
        try:
            info = await asyncio.to_thread(game_watch.detect)
        except Exception:
            return False
        return bool((info or {}).get("running"))

    async def _digest_tick(self) -> None:
        """M8：到点了就把这段时间的活动压成一句写进 livingmemory。

        只写「这一句」，不写每条原始采样——否则记忆图谱会被流水账冲掉。
        """
        if not self.cfg.activity_digest:
            return
        now = time.time()
        for identity_id in self.activity.identities():
            if not self._digest.due(identity_id, now):
                continue
            history = self.activity.history(identity_id)
            text = self._digest.take(identity_id, history, now)
            if not text:
                continue
            umo = identity_to_umo(identity_id)
            if not umo:
                continue
            # 用 LLM 把活动理解成「主人在干什么」的人话；失败则回退到上面的规则汇总
            llm_text = await self._llm_summarize_activity(umo, history)
            if llm_text:
                text = llm_text
            if _same_activity(self._last_activity_text.get(identity_id, ""), text):
                logger.debug("[coop_bridge] 活动汇总与上一条实质重复，跳过: %s", text[:60])
                continue
            ok = False
            try:
                ok = await mirror_note(
                    self.context,
                    umo,
                    text,
                    kind="activity_digest",
                    importance=0.3,
                    logger=logger,
                )
            except Exception as exc:
                # 记忆插件出问题（向量化失败、图谱索引报错等）绝不能拖垮整个机器人，
                # 这次汇总丢掉就好，下个周期还会再试。
                logger.warning("[coop_bridge] 活动汇总写记忆失败，跳过本次: %s", exc)
                continue
            if ok:
                self._last_activity_text[identity_id] = text
                logger.info(
                    "[coop_bridge] 活动已汇总进记忆: %s · %s",
                    _mask(identity_id.split(":")[-1]),
                    text[:80],
                )
            else:
                logger.debug("[coop_bridge] 活动汇总未写入记忆（记忆插件不可用）: %s", text[:60])

    async def _llm_summarize_activity(self, umo: str, history: list) -> str | None:
        """用 LLM 把活动样本理解成「主人在干什么」的一句话；失败返回 None。

        这是对规则汇总（activity_digest.collapse）的增强：规则只报「在用 XX 程序」，
        LLM 能结合进程名 + 窗口标题推断出「在写代码 / 在聊天 / 在看视频」这种人话。
        任何失败都回退到规则汇总，绝不让它拖垮主流程。
        """
        entries = [e for e in (history or []) if isinstance(e, dict)][-12:]
        if len(entries) < 3:
            return None
        lines: list[str] = []
        for entry in entries:
            ts = entry.get("ts") or 0
            if ts <= 0:
                continue
            window = entry.get("window") if isinstance(entry.get("window"), dict) else {}
            process = str(window.get("process") or "").strip()
            title = str(window.get("title") or "").strip()
            clock = time.strftime("%H:%M", time.localtime(ts))
            label = f"- {clock} {process or '(未知程序)'}"
            if title and title.lower() != process.lower():
                label += f"（{title[:40]}）"
            # 空闲时长必须喂给模型：不喂的话它只看到「前台是 XX 程序」，
            # 就会把「人走了、屏幕还开着」笃定地说成「主人一直在做XX」——那是假记忆。
            try:
                idle = float(entry.get("idle_seconds") or 0)
            except (TypeError, ValueError):
                idle = 0.0
            if idle >= AWAY_IDLE_SECONDS:
                label += f" 〔此刻已空闲 {int(idle // 60)} 分钟＝人不在电脑前〕"
            elif idle >= 60:
                label += f" 〔空闲 {int(idle // 60)} 分钟〕"
            lines.append(label)
        if not lines:
            return None
        samples = "\n".join(lines)

        system = (
            "你是桌宠，负责把主人的电脑活动总结成一句简短的人话。"
            "根据进程名和窗口标题推断主人在具体做什么（写代码、聊天、看视频、玩游戏、写文档等），"
            "不要只报程序名。"
            "最重要的一条：采样末尾若标了「空闲 X 分钟」，表示那一刻人没在电脑前（只是屏幕还开着）。"
            "如果大半采样都标着空闲，你必须如实说「主人不在电脑前」，可以顺带提一句屏幕停在哪个程序上；"
            "绝对不许说成「主人一直在做某事」——人不在就是人不在，不要替他编他在干什么。"
        )
        prompt = (
            f"以下是一段时间内主人前台窗口的采样：\n{samples}\n\n"
            "先数一下标了「空闲」的采样占多少：占多数就先说主人不在电脑前；"
            "否则再按进程名和窗口标题说他在干什么。输出格式："
            "【电脑活动】HH:MM–HH:MM <一句话>。只输出这一句，不要任何解释。"
        )

        provider = None
        try:
            provider = self.context.get_using_provider(umo=umo)
        except Exception:
            return None
        if provider is None:
            return None
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=system, contexts=[]),
                timeout=30,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            return text or None
        except Exception as exc:
            logger.debug("[coop_bridge] 活动 LLM 语义化失败，回退规则汇总: %s", exc)
            return None

    async def _watch_deferred(self):
        """每 10 秒看一次：活动汇总、攒着话补发、正在催的继续推进。"""
        while self._running:
            await asyncio.sleep(10)
            if not self._running:
                break
            try:
                await self._digest_tick()
            except Exception as exc:
                logger.warning("[coop_bridge] 活动汇总异常: %s", exc)
            if len(self._deferred) == 0:
                if self._nagging:
                    try:
                        await self._nag_tick()
                    except Exception as exc:
                        logger.warning("[coop_bridge] 催人异常: %s", exc)
                continue
            try:
                if await self._game_running():
                    continue
                await self._flush_deferred()
            except Exception as exc:
                logger.warning("[coop_bridge] 补发检查异常: %s", exc)
            try:
                await self._nag_tick()
            except Exception as exc:
                logger.warning("[coop_bridge] 催人异常: %s", exc)

    async def _flush_deferred(self) -> int:
        """把攒下的话补发给桌宠；对方中途回话的那条直接作废。返回补发条数。"""
        sent_count = 0
        for umo in list(self._deferred.pending()):
            if await self._game_running():
                break
            item = self._deferred.take(umo)
            if not item:
                continue
            count = self._unanswered_count(umo)
            if count < self.cfg.stunt_after:
                logger.info(
                    "[coop_bridge] 游戏已关闭，但对方已回复，丢弃攒下的话: %s",
                    (umo or "").split(":")[-1],
                )
                continue
            meta = dict(item.get("meta") or {})
            meta["unanswered"] = count
            meta["stunt"] = random.choice(STUNT_ACTIONS)
            ok = await route_to_outbox(self.store, self.server.push, umo, item["text"], meta)
            if ok:
                sent_count += 1
                self._start_nag(umo)
                logger.info(
                    "[coop_bridge] 游戏已关闭，补发催人消息: %s（动作 %s）",
                    (umo or "").split(":")[-1],
                    meta["stunt"],
                )
        return sent_count

    def _start_nag(self, umo: str) -> None:
        """开始「一直走过来」：记下起点，后面由 _nag_tick 每 45 秒推一次。"""
        if not self.cfg.stunt_repeat:
            return
        now = time.time()
        self._nagging[umo] = {"started": now, "last": now}

    def _stop_nag(self, umo: str) -> None:
        self._nagging.pop(umo, None)

    async def _nag_tick(self) -> None:
        """推进每一次「走过来」：对方回复就停，超过 15 分钟也停。"""
        if not self._nagging:
            return
        now = time.time()
        for umo in list(self._nagging):
            state = self._nagging.get(umo)
            if not state:
                continue
            if now - float(state.get("started", now)) > NAG_MAX_SECONDS:
                self._nagging.pop(umo, None)
                logger.info(
                    "[coop_bridge] 已催 %d 分钟没人应，先停下: %s",
                    NAG_MAX_SECONDS // 60,
                    (umo or "").split(":")[-1],
                )
                continue
            if self._unanswered_count(umo) <= 0:
                self._nagging.pop(umo, None)
                logger.info(
                    "[coop_bridge] 对方已回复，停止催: %s",
                    (umo or "").split(":")[-1],
                )
                continue
            if now - float(state.get("last", 0)) < NAG_INTERVAL_SECONDS:
                continue
            if await self._game_running():
                continue
            state["last"] = now
            meta = {
                "kind": "proactive",
                "delivered": True,
                "stunt": NAG_ACTION,
                "unanswered": self._unanswered_count(umo),
            }
            ok = await route_to_outbox(
                self.store, self.server.push, umo, "（走过来看看主人还在不在）", meta
            )
            if ok:
                logger.info(
                    "[coop_bridge] 还是没回，让桌宠再走一次: %s",
                    (umo or "").split(":")[-1],
                )

    @_llm_request_hook()
    async def _inject_activity(self, event, req):
        """M8：把最近一条「主人在干什么」塞进上下文，让回复能自然带出来。"""
        try:
            if not self.cfg.activity_enabled:
                return
            umo = getattr(event, "unified_msg_origin", "") or ""
            identity = BridgeStore.identity_from_umo(umo)
            if identity is None:
                return
            text = self.activity.describe(identity["identity_id"])
            if not text:
                return
            contexts = getattr(req, "contexts", None)
            if isinstance(contexts, list):
                contexts.append(
                    {"role": "system", "content": [{"type": "text", "text": text}]}
                )
        except Exception as exc:
            logger.debug("[coop_bridge] 活动注入跳过: %s", exc)

    async def _record_chat(self, identity_id: str, items: list) -> int:
        """M6 回调：把 Coopanion 里的对话写进该身份的会话历史。

        写进的是同一个 QQ 会话（`<platform>:FriendMessage:<QQ>`），所以记忆插件
        能像平时一样把这些内容当素材吸收；Coo 那边的人格与行为不受影响。

        只写「一问一答都齐」的整轮，半截对话直接放弃——避免把空的 assistant
        消息塞进上下文让模型误以为自己在冷场。
        """
        platform_id, _, participant_id = (identity_id or "").partition(":")
        if not platform_id or not participant_id:
            raise ValueError("身份格式不对")
        umo = f"{platform_id}:FriendMessage:{participant_id}"

        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            raise RuntimeError("当前 AstrBot 版本没有 conversation_manager")

        cid = await manager.get_curr_conversation_id(umo)
        if not cid:
            cid = await manager.new_conversation(umo, platform_id=platform_id)

        accepted = 0
        mirrored = 0
        for item in items:
            user_text = str(item.get("user") or "").strip()
            reply_text = str(item.get("assistant") or "").strip()
            if not user_text or not reply_text:
                continue
            await manager.add_message_pair(
                cid,
                {"role": "user", "content": [{"type": "text", "text": user_text}]},
                {"role": "assistant", "content": [{"type": "text", "text": reply_text}]},
            )
            accepted += 1
            if self.cfg.mirror_memory:
                if await mirror_chat(self.context, umo, user_text, reply_text, logger):
                    mirrored += 1
        if accepted:
            logger.info(
                "[coop_bridge] 对话已写入会话历史: %s（%d 轮，其中 %d 轮进 livingmemory）",
                (umo or "").split(":")[-1],
                accepted,
                mirrored,
            )
        return accepted

    async def _record_video(self, identity_id: str, platform: str, logged_in: bool, cards: list) -> int:
        """M20/M21 回调：把刷到的一批视频卡片分析成一句话，写进 livingmemory。

        登录 = 按「用户喜好」存；未登录 = 按「学习记忆」存。
        """
        platform_id, _, participant_id = (identity_id or "").partition(":")
        if not platform_id or not participant_id:
            raise ValueError("身份格式不对")
        umo = f"{platform_id}:FriendMessage:{participant_id}"

        text = await self._analyze_video_cards(umo, platform, cards)
        if not text:
            return 0
        is_bili = platform == "bilibili"
        if logged_in:
            kind = "bilibili_preference" if is_bili else "douyin_preference"
            topics = ["B站喜好"] if is_bili else ["抖音喜好"]
        else:
            kind = "bilibili_learning" if is_bili else "douyin_learning"
            topics = ["B站学习"] if is_bili else ["抖音学习"]
        ok = await mirror_note(
            self.context,
            umo,
            text,
            kind=kind,
            importance=0.5,
            logger=logger,
            topics=topics,
        )
        if ok:
            logger.info(
                "[coop_bridge] 刷%s内容已写记忆: %s（%s）",
                "B站" if is_bili else "抖音",
                text[:60],
                "喜好" if logged_in else "学习",
            )
        return 1 if ok else 0

    async def _analyze_video_cards(self, umo: str, platform: str, cards: list) -> str:
        """用 LLM 看一批卡片（标题 + 封面图），拟人化总结成一句话。"""
        provider = None
        try:
            provider = self.context.get_using_provider(umo=umo)
        except Exception:
            return ""
        if provider is None:
            return ""

        name = "B站" if platform == "bilibili" else "抖音"
        lines: list[str] = []
        covers: list[str] = []
        for card in cards[:15]:
            text = str(card.get("text") or "").strip()
            if not text:
                continue
            lines.append(f"{len(lines) + 1}. {text[:120]}")
            cover = str(card.get("cover") or "").strip()
            if cover:
                covers.append(cover)
        if not lines:
            return ""

        system = (
            f"你是桌宠「大肥鱼」。主人让你刷{name}玩，你把刷到的视频看了一遍。"
            "根据标题和封面图，判断这批视频主要是什么内容（比如美女、游戏-战争雷霆、美食、影视等）。"
        )
        prompt = (
            f"下面是我刚刷到的一批{name}视频（编号+文案），结合封面图判断内容：\n"
            + "\n".join(lines)
            + "\n\n请用一两句拟人的话，像一条刚刷完视频的小鱼在跟主人汇报，"
            "说清楚这批视频主要是些什么内容（点名几个具体类型，比如「刷到好几条战争雷霆的游戏、还有美女和美食」）。"
            "不要逐条编号罗列，不要解释。"
        )
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt=system,
                    image_urls=covers[:15],
                ),
                timeout=60,
            )
            return (getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:
            logger.debug("[coop_bridge] %s卡片分析失败: %s", name, exc)
            return ""

    async def terminate(self):
        self._running = False
        uninstall_proactive_hook()
        self._proactive_attached = False
        await self.server.stop()
        logger.info("[coop_bridge] 已卸载")

    # ---------- HTTP: 配对码换令牌 ----------

    def _register_pair_api(self):
        """注册 /bridge/pair。注册失败只告警，不能拖垮插件加载。"""
        try:
            self.context.register_web_api(
                "/bridge/pair",
                self._api_pair,
                ["POST"],
                "Coopanion 机器配对：用一次性配对码换长期机器令牌",
            )
        except Exception as exc:  # pragma: no cover - 仅在异常环境触发
            logger.warning("[coop_bridge] 注册 /bridge/pair 失败: %s", exc)

    async def _api_pair(self, request):
        from astrbot.api.web import error_response, json_response

        payload = {}
        try:
            payload = await request.json()
        except Exception:
            try:
                payload = dict(getattr(request, "query_params", {}) or {})
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        try:
            device = await self.pair.redeem(
                str(payload.get("code", "") or ""),
                str(payload.get("device_name", "") or ""),
            )
        except PairError as exc:
            return error_response(str(exc), status_code=400)
        logger.info(
            "[coop_bridge] 新设备配对: %s (%s)", device["device_id"], device["name"]
        )
        return json_response(
            {
                "token": device["token"],
                "device_id": device["device_id"],
                "name": device["name"],
                "identity_label": self._mask_identity(device["identity_id"]),
            }
        )

    # ---------- 私聊入口 ----------

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        """所有私聊消息的总入口：未绑定走绑定流程，已绑定只接管解绑指令。"""
        if not self.cfg.enabled:
            return
        identity = self.store.resolve_identity(event)
        if identity is None:
            return
        text = self._plain_text(event)
        if not text:
            return

        record = await self.store.get_identity(identity["identity_id"])
        if record is not None:
            async for result in self._handle_bound(event, identity["identity_id"], text):
                yield result
            return
        async for result in self._handle_unbound(event, identity, text):
            yield result

    # ---------- 已绑定身份 ----------

    async def _handle_bound(self, event: AstrMessageEvent, identity_id: str, text: str):
        """已绑定：接管解绑/配对/设备指令，其余消息放行给主对话链路。"""

        # 1) 解绑（优先，避免与配对指令互相吞掉）
        if text == UNBIND_WORD:
            await self.store.put_pending_unbind(identity_id)
            yield event.plain_result(self.cfg.unbind_prompt)
            event.stop_event()
            return
        pending = await self.store.get_pending_unbind(identity_id, self.cfg.bind_pending_ttl)
        if pending is not None:
            if text == UNBIND_CONFIRM_WORD:
                revoked = await self.pair.revoke_all(identity_id)
                await self.store.delete_identity(identity_id)
                await self.store.clear_pending_unbind(identity_id)
                logger.info(
                    "[coop_bridge] 身份已注销: %s（同时吊销 %d 台设备）",
                    identity_id,
                    revoked,
                )
                yield event.plain_result(self.cfg.unbind_done_text.format(devices=revoked))
            else:
                await self.store.clear_pending_unbind(identity_id)
                yield event.plain_result("已取消解绑。")
            event.stop_event()
            return

        # 2) 取配对码
        if text == PAIR_WORD:
            try:
                code = await self.pair.issue_code(identity_id)
            except PairError as exc:
                yield event.plain_result(str(exc))
            else:
                yield event.plain_result(
                    self.cfg.pair_code_text.format(
                        code=code, minutes=self.cfg.pair_code_ttl // 60
                    )
                )
            event.stop_event()
            return

        # 3) 设备列表
        if text == DEVICES_WORD:
            devices = await self.pair.list_devices(identity_id)
            if not devices:
                yield event.plain_result(self.cfg.device_empty_text)
            else:
                lines = "\n".join(
                    "· {}（{}）".format(
                        d.get("name") or "未命名", self._stamp(d.get("bound_at"))
                    )
                    for d in devices
                )
                yield event.plain_result(
                    self.cfg.device_list_text.format(count=len(devices), lines=lines)
                )
            event.stop_event()
            return

        # 4) 取消配对 [名字]
        if text == REVOKE_WORD or text.startswith(REVOKE_WORD):
            selector = text[len(REVOKE_WORD):].strip()
            if not selector:
                devices = await self.pair.list_devices(identity_id)
                if not devices:
                    yield event.plain_result(self.cfg.device_empty_text)
                else:
                    names = "、".join((d.get("name") or "未命名") for d in devices)
                    yield event.plain_result(
                        self.cfg.revoke_need_name_text.format(names=names)
                    )
                event.stop_event()
                return
            removed = await self.pair.revoke(identity_id, selector)
            if removed is None:
                yield event.plain_result(self.cfg.revoke_miss_text.format(name=selector))
            else:
                logger.info("[coop_bridge] 设备已吊销: %s", removed["device_id"])
                yield event.plain_result(
                    self.cfg.revoke_done_text.format(name=removed.get("name") or "未命名")
                )
            event.stop_event()
            return

        # 其他任何消息：不 stop_event，交给 AstrBot 主回复链

    # ---------- 未绑定身份 ----------

    async def _handle_unbound(self, event: AstrMessageEvent, identity: dict, text: str):
        """未绑定：进入一次性确认流程。确认前拦住消息，不喂给主链路。"""
        identity_id = identity["identity_id"]
        qq = identity["participant_id"]
        pending = await self.store.get_pending_bind(identity_id, self.cfg.bind_pending_ttl)

        if pending is not None and text == CONFIRM_WORD:
            nickname = self._sender_name(event)
            await self.store.commit_bind(identity, nickname)
            await self.store.clear_pending_bind(identity_id)
            logger.info("[coop_bridge] 新身份绑定: %s (%s)", identity_id, nickname or "无昵称")
            yield event.plain_result(self.cfg.bind_done_text.format(qq=qq))
            event.stop_event()
            return

        if pending is not None and text == REJECT_WORD:
            await self.store.clear_pending_bind(identity_id)
            yield event.plain_result(self.cfg.bind_reject_text)
            event.stop_event()
            return

        # 无待确认记录，或用户在流程里说了别的：发/重发提示，刷新有效期
        await self.store.put_pending_bind(identity)
        yield event.plain_result(self.cfg.bind_prompt.format(qq=qq))
        event.stop_event()

    # ---------- 工具 ----------

    @staticmethod
    def _plain_text(event: AstrMessageEvent) -> str:
        try:
            return (event.get_message_str() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _sender_name(event: AstrMessageEvent) -> str:
        try:
            return (event.get_sender_name() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _stamp(ts) -> str:
        try:
            return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
        except (TypeError, ValueError):
            return "未知时间"

    @staticmethod
    def _mask_identity(identity_id: str) -> str:
        """对外只给打码后的身份标签，不把完整 QQ 号交给机器端。"""
        _, _, participant = identity_id.partition(":")
        if len(participant) <= 5:
            return participant
        return f"{participant[:3]}*****{participant[-2:]}"
