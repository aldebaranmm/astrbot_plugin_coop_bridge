"""桥接 HTTP 服务（M4）。

独立于 AstrBot 仪表盘跑在自己的端口（默认 6190），只绑 127.0.0.1：
  - 公网暴露靠 frp 隧道，不由本服务直接面对外网；
  - 仪表盘（带管理员登录）绝不穿透，这是硬边界。

端点：
  GET  /bridge/health     无需鉴权，探活 + 适配器状态
  POST /bridge/pair       无需鉴权，配对码换机器令牌
  GET  /bridge/identity   令牌鉴权，返回该机器所属身份（QQ 号打码）
  POST /bridge/recall     令牌鉴权，检索 AstrBot 侧记忆
  GET  /bridge/inbox      令牌鉴权，拉取待推送给该机器的主动消息（M3 写入）
  POST /bridge/chat       令牌鉴权，Coopanion 把桌宠里的对话回传到该身份的会话历史（M6）
  POST /bridge/push       令牌鉴权，往该身份的待推队列塞一条（远程下发指令/通知）
  POST /bridge/activity   令牌鉴权，Coopanion 上报「主人现在在干什么」（M8）
  GET  /bridge/activity   令牌鉴权，读最近一条活动（桌宠自己查）
"""

from __future__ import annotations

import time
from typing import Any, Optional

from aiohttp import web

# 单次 inbox 最多返回条数，避免一次拉爆上下文
INBOX_BATCH = 5


class BridgeServer:
    def __init__(self, cfg, store, pair, memory, logger, version: str = "0.0.0", on_chat=None, activity=None, on_video=None):
        self.cfg = cfg
        self._store = store
        self._pair = pair
        self._memory = memory
        self._log = logger
        self._version = version
        # M6：把 Coopanion 回传的对话写进会话历史；由插件本体注入，缺失时该端点返回 501
        self._on_chat = on_chat
        # M8：用户活动（前台窗口 / 可选的文件名），按身份隔离
        self._activity = activity
        # M20/M21：桌宠刷视频（抖音/B站）回传的一批卡片；由插件本体注入
        self._on_video = on_video
        self._runner: Optional[web.AppRunner] = None
        self._site = None
        # M3 写入、M4 消费的待推送队列：{identity_id: [{text, meta, ts}]}
        self._outbox: dict[str, list] = {}

    # ---------- 生命周期 ----------

    async def start(self) -> bool:
        """启动服务。端口被占等异常只告警，不影响插件本体加载。"""
        if not getattr(self.cfg, "server_enabled", True):
            if self._log is not None:
                self._log.info("[coop_bridge] 桥接服务已在配置中关闭")
            return False
        app = web.Application()
        app.router.add_get("/bridge/health", self._health)
        app.router.add_post("/bridge/pair", self._pair_ep)
        app.router.add_get("/bridge/identity", self._identity)
        app.router.add_post("/bridge/recall", self._recall)
        app.router.add_get("/bridge/inbox", self._inbox)
        app.router.add_post("/bridge/chat", self._chat)
        app.router.add_post("/bridge/push", self._push_ep)
        app.router.add_post("/bridge/activity", self._activity_post)
        app.router.add_get("/bridge/activity", self._activity_get)
        app.router.add_post("/bridge/douyin", self._douyin_post)
        app.router.add_post("/bridge/video", self._video_post)

        self._runner = web.AppRunner(app)
        try:
            await self._runner.setup()
            self._site = web.TCPSite(
                self._runner,
                self.cfg.server_bind,
                self.cfg.server_port,
            )
            await self._site.start()
        except Exception as exc:
            if self._log is not None:
                self._log.warning(
                    "[coop_bridge] 桥接服务启动失败（端口 %s 可能被占用）: %s",
                    self.cfg.server_port,
                    exc,
                )
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None
            return False

        if self._log is not None:
            self._log.info(
                "[coop_bridge] 桥接服务已启动: http://%s:%s/bridge/health",
                self.cfg.server_bind,
                self.cfg.server_port,
            )
        return True

    async def stop(self) -> None:
        if self._runner is None:
            return
        try:
            await self._runner.cleanup()
        except Exception as exc:
            if self._log is not None:
                self._log.warning("[coop_bridge] 桥接服务停止异常: %s", exc)
        self._runner = None
        self._site = None

    # ---------- M3 写入口 ----------

    def push(self, identity_id: str, text: str, meta: Optional[dict] = None) -> None:
        """M3 主动消息改道：把要推给该身份的消息压进队列，由机器来拉。"""
        if not identity_id or not text:
            return
        queue = self._outbox.setdefault(identity_id, [])
        queue.append({"text": text, "meta": meta or {}, "ts": time.time()})
        # 防止某台机器长期离线导致队列无限增长
        if len(queue) > 50:
            del queue[: len(queue) - 50]

    # ---------- 端点 ----------

    async def _health(self, request):
        # 这个端点无需鉴权、且可能经 frp 暴露在公网：只报开关与可用性，
        # 绝不回本机绝对路径（会泄露 Windows 用户名与插件目录结构）。
        return self._json(
            {
                "ok": True,
                "service": "astrbot_plugin_coop_bridge",
                "version": self._version,
                "time": time.time(),
                "sources": self._public_sources(),
            }
        )

    def _public_sources(self) -> dict:
        raw = self._memory.status() or {}
        safe = {}
        for name, info in raw.items():
            if isinstance(info, dict):
                safe[name] = {
                    key: value for key, value in info.items() if key not in ("path", "db_path", "file")
                }
            else:
                safe[name] = info
        return safe

    async def _pair_ep(self, request):
        payload = await self._payload(request)
        try:
            device = await self._pair.redeem(
                str(payload.get("code", "") or ""),
                str(payload.get("device_name", "") or ""),
            )
        except Exception as exc:
            return self._json({"ok": False, "error": str(exc)}, status=400)
        if self._log is not None:
            self._log.info(
                "[coop_bridge] 新设备配对: %s (%s)", device["device_id"], device["name"]
            )
        return self._json(
            {
                "ok": True,
                "token": device["token"],
                "device_id": device["device_id"],
                "name": device["name"],
                "identity_label": self._mask(device["identity_id"]),
            }
        )

    async def _identity(self, request):
        device = await self._auth(request)
        if device is None:
            return self._json({"ok": False, "error": "令牌无效或已吊销"}, status=401)
        identity = await self._store.get_identity(device["identity_id"])
        participant = device["identity_id"].partition(":")[2]
        return self._json(
            {
                "ok": True,
                "identity_label": self._mask(device["identity_id"]),
                "device_name": device.get("name") or "",
                "nickname": (identity or {}).get("nickname") or "",
                "bound_at": (identity or {}).get("bound_at"),
                "device_bound_at": device.get("bound_at"),
                "participant_hint": participant[:3] + "***" if len(participant) > 3 else "",
            }
        )

    async def _recall(self, request):
        device = await self._auth(request)
        if device is None:
            return self._json({"ok": False, "error": "令牌无效或已吊销"}, status=401)
        payload = await self._payload(request)
        query = str(payload.get("query", "") or "")
        try:
            limit = int(payload.get("limit", 0) or 0)
        except (TypeError, ValueError):
            limit = 0
        limit = min(limit or self.cfg.recall_limit, 50)

        identity_id = device["identity_id"]
        platform_id, _, participant_id = identity_id.partition(":")
        result = await self._memory.recall(
            participant_id=participant_id,
            platform_id=platform_id,
            query=query,
            limit=limit,
        )
        result["ok"] = True
        result["identity_label"] = self._mask(identity_id)
        return self._json(result)

    async def _push_ep(self, request):
        """往该身份的待推送队列塞一条。

        身份只认令牌，所以谁也不能往别人的机器里塞东西。
        用于远程下发指令、手动测试（比如让某台机器的桌宠说一句话）。
        """
        device = await self._auth(request)
        if device is None:
            return self._json({"ok": False, "error": "令牌无效或已吊销"}, status=401)
        payload = await self._payload(request)
        text = str(payload.get("text") or "").strip()
        if not text:
            return self._json({"ok": False, "error": "缺少 text"}, status=400)
        raw_meta = payload.get("meta")
        meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
        meta["kind"] = str(meta.get("kind") or "manual")
        meta["via"] = "push"
        identity_id = device["identity_id"]
        self.push(identity_id, text[:2000], meta)
        pending = len(self._outbox.get(identity_id, []))
        if self._log is not None:
            self._log.info(
                "[coop_bridge] 手动推送 1 条给 %s：%s（队列 %d）",
                self._mask(identity_id),
                text[:40],
                pending,
            )
        return self._json({"ok": True, "pending": pending})

    async def _inbox(self, request):
        device = await self._auth(request)
        if device is None:
            return self._json({"ok": False, "error": "令牌无效或已吊销"}, status=401)
        identity_id = device["identity_id"]
        queue = self._outbox.get(identity_id, [])
        batch = queue[:INBOX_BATCH]
        if batch:
            del queue[: len(batch)]
        return self._json({"ok": True, "messages": batch, "pending": len(queue)})

    async def _chat(self, request):
        """M6：Coopanion 把桌宠里的对话回传过来，写进该身份的会话历史。

        items: [{"user": "主人说的话", "assistant": "桌宠回的话", "ts": 可选}]
        只收文字对，不接受工具调用、触摸反应等其它东西。
        """
        device = await self._auth(request)
        if device is None:
            return self._json({"ok": False, "error": "令牌无效或已吊销"}, status=401)
        payload = await self._payload(request)
        raw = payload.get("items")
        if not isinstance(raw, list) or not raw:
            return self._json({"ok": False, "error": "items 不能为空"}, status=400)

        items: list[dict] = []
        for entry in raw[:20]:
            if not isinstance(entry, dict):
                continue
            user_text = str(entry.get("user", "") or "").strip()
            reply_text = str(entry.get("assistant", "") or "").strip()
            if not user_text or not reply_text:
                # 一问一答都齐了才写；只有一半的会污染上下文
                continue
            items.append({"user": user_text[:2000], "assistant": reply_text[:2000]})
        if not items:
            return self._json({"ok": True, "accepted": 0, "skipped": len(raw)})

        if self._on_chat is None:
            return self._json({"ok": False, "error": "本端未接入对话记录"}, status=501)
        try:
            accepted = await self._on_chat(device["identity_id"], items)
        except Exception as exc:
            if self._log is not None:
                self._log.warning("[coop_bridge] 对话写入失败: %s", exc)
            return self._json({"ok": False, "error": f"写入失败：{exc}"}, status=500)
        if self._log is not None:
            self._log.info(
                "[coop_bridge] 已记录 Coopanion 对话: %s（%s 轮）",
                self._mask(device["identity_id"]),
                accepted,
            )
        return self._json({"ok": True, "accepted": accepted})

    async def _activity_post(self, request):
        """M8：上报一条活动。只收窗口与文件名的摘要，不收文件内容。"""
        device = await self._auth(request)
        if device is None:
            return self._json({"ok": False, "error": "令牌无效或已吊销"}, status=401)
        if self._activity is None:
            return self._json({"ok": False, "error": "本端未启用活动记录"}, status=501)
        payload = await self._payload(request)
        try:
            entry = self._activity.record(device["identity_id"], payload)
        except Exception as exc:
            return self._json({"ok": False, "error": f"记录失败：{exc}"}, status=400)
        if self._log is not None:
            window = (entry.get("window") or {}).get("title") or "(无窗口)"
            self._log.info(
                "[coop_bridge] 活动上报: %s · %s", self._mask(device["identity_id"]), window[:60]
            )
        return self._json({"ok": True, "ts": entry.get("ts")})

    async def _activity_get(self, request):
        """M8：桌宠查最近一条活动。"""
        device = await self._auth(request)
        if device is None:
            return self._json({"ok": False, "error": "令牌无效或已吊销"}, status=401)
        if self._activity is None:
            return self._json({"ok": False, "error": "本端未启用活动记录"}, status=501)
        identity_id = device["identity_id"]
        return self._json(
            {
                "ok": True,
                "latest": self._activity.latest(identity_id),
                "describe": self._activity.describe(identity_id),
                "history": self._activity.history(identity_id)[-5:],
            }
        )

    async def _douyin_post(self, request):
        """旧端点：桌宠 0.7.0 及更早版本刷抖音回传，platform 固定 douyin。"""
        return await self._video_post(request, "douyin")

    async def _video_post(self, request, default_platform=None):
        """M20/M21：桌宠刷视频（抖音/B站）回传的一批卡片。只收标题/作者/点赞/封面图 URL，不收图片内容。"""
        device = await self._auth(request)
        if device is None:
            return self._json({"ok": False, "error": "令牌无效或已吊销"}, status=401)
        if self._on_video is None:
            return self._json({"ok": False, "error": "本端未接入刷视频记录"}, status=501)
        payload = await self._payload(request)
        platform = str(payload.get("platform") or default_platform or "douyin")
        if platform not in ("douyin", "bilibili"):
            platform = "douyin"
        logged_in = bool(payload.get("loggedIn"))
        raw = payload.get("cards")
        if not isinstance(raw, list) or not raw:
            return self._json({"ok": True, "accepted": 0})
        cards: list[dict] = []
        for entry in raw[:30]:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text", "") or "").strip()
            if not text:
                continue
            cards.append(
                {
                    "text": text[:300],
                    "author": str(entry.get("author", "") or "")[:60],
                    "like": str(entry.get("like", "") or "")[:20],
                    "cover": str(entry.get("cover", "") or "")[:500],
                }
            )
        if not cards:
            return self._json({"ok": True, "accepted": 0})
        try:
            accepted = await self._on_video(device["identity_id"], platform, logged_in, cards)
        except Exception as exc:
            if self._log is not None:
                self._log.warning("[coop_bridge] 刷视频记录写入失败: %s", exc)
            return self._json({"ok": False, "error": f"写入失败：{exc}"}, status=500)
        if self._log is not None:
            self._log.info(
                "[coop_bridge] 已记录刷%s卡片: %s（%s 条，%s）",
                "B站" if platform == "bilibili" else "抖音",
                self._mask(device["identity_id"]),
                accepted,
                "已登录" if logged_in else "未登录",
            )
        return self._json({"ok": True, "accepted": accepted})

    # ---------- 内部 ----------

    async def _auth(self, request) -> Optional[dict]:
        """Bearer <token> 或 X-Bridge-Token: <token>。"""
        token = ""
        header = request.headers.get("Authorization", "") or ""
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
        if not token:
            token = (request.headers.get("X-Bridge-Token", "") or "").strip()
        if not token:
            token = (request.query.get("token", "") or "").strip()
        if not token:
            return None
        device = await self._pair.device_by_token(token)
        if device is not None and self._log is not None:
            try:
                await self._pair.touch(device["device_id"])
            except Exception:
                pass
        return device

    @staticmethod
    async def _payload(request) -> dict:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            try:
                return dict(request.query)
            except Exception:
                return {}

    @staticmethod
    def _mask(identity_id: str) -> str:
        _, _, participant = identity_id.partition(":")
        if len(participant) <= 5:
            return participant
        return f"{participant[:3]}*****{participant[-2:]}"

    @staticmethod
    def _json(data: dict, status: int = 200) -> web.Response:
        return web.json_response(data, status=status)
