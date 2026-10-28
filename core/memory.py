"""记忆检索中心（M4）。

把 AstrBot 侧各家记忆插件的数据读出来，统一成一份检索结果给 Coopanion。

设计原则：
  1. 只读。绝不写、绝不改别人的库。
  2. 隔离。任一家插件读失败，只影响它自己那一段，其余照常返回。
  3. 不猜 schema。字段可能缺失（插件升级会改），逐层 try 兜住。
  4. 身份隔离。只取属于当前身份的记录，公共记忆（public）额外并入。

当前适配器（按价值排序）：
  livingmemory     plugin_data/astrbot_plugin_livingmemory/livingmemory.db
                   → graph_entries：401 条带 session_id 的事实记忆，主力数据源
  angel_memory     plugin_data/astrbot_plugin_angel_memory/memory_center/index/simple_memory.db
  self_learning    plugin_data/astrbot_plugin_self_learning/messages.db
                   → 注意：它已声明把长期记忆委托给 livingmemory，这里只取好感度/黑话
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Optional

# 检索时忽略的虚词，避免 LIKE '%的%' 这种噪声命中
STOPWORDS = {
    "的", "了", "吗", "呢", "啊", "吧", "是", "我", "你", "他", "她", "它",
    "这", "那", "什么", "怎么", "为什么", "有", "在", "和", "就", "不", "也",
    "请", "帮", "一下", "一个", "the", "a", "an", "is", "are", "to", "of",
}

MIN_TOKEN_LEN = 2
MAX_TOKENS = 6


def tokenize(query: str) -> list[str]:
    """把问题切成检索词：中文按字组、英文数字按词，过滤虚词和过短片段。"""
    if not query:
        return []
    raw = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", query)
    tokens: list[str] = []
    for piece in raw:
        if re.match(r"[A-Za-z0-9_]+", piece):
            if len(piece) >= MIN_TOKEN_LEN and piece.lower() not in STOPWORDS:
                tokens.append(piece)
            continue
        # 中文串：整串 + 二元切分，提高短词命中率
        if len(piece) >= MIN_TOKEN_LEN and piece not in STOPWORDS:
            tokens.append(piece)
        for i in range(len(piece) - 1):
            bigram = piece[i : i + 2]
            if bigram not in STOPWORDS:
                tokens.append(bigram)
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:MAX_TOKENS]


def _score(text: str, tokens: list[str]) -> float:
    """命中词数 / 总词数，再给一点长度惩罚（太长多为噪声段落）。"""
    if not tokens:
        return 0.0
    hit = sum(1 for t in tokens if t in text)
    if not hit:
        return 0.0
    base = hit / len(tokens)
    length_penalty = 1.0 if len(text) <= 400 else 0.85
    return round(base * length_penalty, 4)


class SqliteReader:
    """只读打开 SQLite，WAL 库也能读；失败则复制一份再读。"""

    def __init__(self, path: Path):
        self.path = path
        self._conn: Optional[sqlite3.Connection] = None
        self._tmp: Optional[Path] = None

    def __enter__(self) -> sqlite3.Connection:
        try:
            self._conn = sqlite3.connect(
                "file:{}?mode=ro".format(str(self.path).replace("?", "%3f")),
                uri=True,
                timeout=2.0,
            )
            self._conn.execute("SELECT 1")
            return self._conn
        except Exception:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
            import shutil
            import tempfile

            tmpdir = Path(tempfile.mkdtemp(prefix="coop_bridge_"))
            self._tmp = tmpdir / self.path.name
            try:
                shutil.copy(self.path, self._tmp)
            except Exception as exc:
                raise RuntimeError("无法读取数据库 {}: {}".format(self.path.name, exc))
            self._conn = sqlite3.connect(str(self._tmp), timeout=2.0)
            return self._conn

    def __exit__(self, *exc_info) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        if self._tmp is not None:
            try:
                self._tmp.unlink(missing_ok=True)
                self._tmp.parent.rmdir()
            except Exception:
                pass
            self._tmp = None


class MemoryHub:
    """按身份检索 AstrBot 侧记忆。"""

    SRC_LIVING = "livingmemory"
    SRC_ANGEL = "angel_memory"
    SRC_SELF = "self_learning"

    def __init__(
        self,
        data_dir: Path,
        enable_angel: bool = True,
        enable_self: bool = True,
        enable_living: bool = True,
        include_group: bool = False,
        logger: Any = None,
    ):
        self.data_dir = Path(data_dir)
        self.enable_angel = enable_angel
        self.enable_self = enable_self
        self.enable_living = enable_living
        # 群聊记忆含他人发言，默认不并入个人档案
        self.include_group = include_group
        self._log = logger
        self._living_db = (
            self.data_dir / "plugin_data" / "astrbot_plugin_livingmemory" / "livingmemory.db"
        )
        self._angel_db = (
            self.data_dir
            / "plugin_data"
            / "astrbot_plugin_angel_memory"
            / "memory_center"
            / "index"
            / "simple_memory.db"
        )
        self._self_db = (
            self.data_dir / "plugin_data" / "astrbot_plugin_self_learning" / "messages.db"
        )

    # ---------- 对外 ----------

    def status(self) -> dict:
        """各适配器可用性，供 /bridge/health 报告。"""
        return {
            self.SRC_LIVING: {
                "enabled": self.enable_living,
                "available": self.enable_living and self._living_db.exists(),
                "path": str(self._living_db) if self.enable_living else "",
            },
            self.SRC_ANGEL: {
                "enabled": self.enable_angel,
                "available": self.enable_angel and self._angel_db.exists(),
                "path": str(self._angel_db) if self.enable_angel else "",
            },
            self.SRC_SELF: {
                "enabled": self.enable_self,
                "available": self.enable_self and self._self_db.exists(),
                "path": str(self._self_db) if self.enable_self else "",
            },
        }

    async def recall(
        self,
        participant_id: str,
        platform_id: str = "",
        query: str = "",
        limit: int = 20,
    ) -> dict:
        """检索。返回 {items, sources, profile, query, tokens}。

        阻塞的 SQLite 操作全部丢到线程池，不卡 AstrBot 主循环。
        """
        tokens = tokenize(query)
        loop = asyncio.get_event_loop()
        tasks: dict[str, asyncio.Future] = {}

        if self.enable_living and self._living_db.exists():
            tasks[self.SRC_LIVING] = loop.run_in_executor(
                None,
                self._recall_living,
                participant_id,
                platform_id,
                tokens,
                limit,
            )
        if self.enable_angel and self._angel_db.exists():
            tasks[self.SRC_ANGEL] = loop.run_in_executor(
                None, self._recall_angel, participant_id, platform_id, tokens, limit
            )
        if self.enable_self and self._self_db.exists():
            tasks[self.SRC_SELF] = loop.run_in_executor(
                None, self._recall_self, participant_id, tokens, limit
            )

        sources: dict[str, dict] = {}
        items: list[dict] = []
        for name, fut in tasks.items():
            try:
                result = await fut
            except Exception as exc:  # 隔离：一家挂了不影响别家
                if self._log is not None:
                    self._log.warning("[coop_bridge] 记忆源 %s 读取失败: %s", name, exc)
                sources[name] = {"ok": False, "error": str(exc), "count": 0}
                continue
            sources[name] = {"ok": True, "error": None, "count": len(result)}
            items.extend(result)

        items.sort(key=lambda x: (-x.get("score", 0.0), -x.get("ts", 0.0)))
        items = items[:limit]

        profile = await loop.run_in_executor(
            None, self._profile, participant_id, platform_id
        )
        return {
            "query": query,
            "tokens": tokens,
            "items": items,
            "sources": sources,
            "profile": profile,
            "generated_at": time.time(),
        }

    # ---------- livingmemory（主力） ----------

    def _recall_living(
        self, participant_id: str, platform_id: str, tokens: list[str], limit: int
    ) -> list[dict]:
        """livingmemory：graph_entries 按 session_id 归属到身份，是记忆主力。

        session_id 形如 "default:FriendMessage:2760546027"，
        与桥接插件的 identity_id 完全同构，所以能精确隔离身份。
        """
        platform = platform_id or "default"
        private_session = "{}:FriendMessage:{}".format(platform, participant_id)

        out: list[dict] = []
        with SqliteReader(self._living_db) as con:
            rows: list[tuple] = []
            # 私聊会话：整段都属于该身份
            try:
                rows.extend(
                    con.execute(
                        "SELECT entry_type, content, metadata, created_at"
                        " FROM graph_entries WHERE session_id = ?",
                        (private_session,),
                    ).fetchall()
                )
            except sqlite3.Error:
                pass
            # 群聊会话：只取内容里直接提到该身份的条目，避免泄漏他人发言
            if self.include_group:
                try:
                    rows.extend(
                        con.execute(
                            "SELECT entry_type, content, metadata, created_at"
                            " FROM graph_entries WHERE session_id LIKE ? AND content LIKE ?",
                            (
                                "{}:GroupMessage:%".format(platform),
                                "%{}%".format(participant_id),
                            ),
                        ).fetchall()
                    )
                except sqlite3.Error:
                    pass

        for entry_type, content, metadata, created in rows:
            text = str(content or "")
            # 前缀清洗：Atom: / Topic ... relates to ... 之类图谱包装语
            for prefix in ("Atom: ", "Topic "):
                if text.startswith(prefix):
                    text = text[len(prefix) :]
            score = _score(text, tokens)
            if score <= 0:
                continue
            importance = 0.0
            try:
                meta = json.loads(metadata or "{}")
                if isinstance(meta, dict):
                    importance = float(meta.get("importance") or 0.0)
            except (TypeError, ValueError):
                pass
            out.append(
                {
                    "source": self.SRC_LIVING,
                    "kind": entry_type or "fact",
                    "text": text.strip(),
                    "importance": round(importance, 3),
                    "score": round(min(1.0, score + importance * 0.3), 4),
                    "ts": 0,
                    "created_at": created or "",
                }
            )
        out.sort(key=lambda x: -x["score"])
        return out[:limit]

    # ---------- angel_memory ----------

    def _recall_angel(
        self, participant_id: str, platform_id: str, tokens: list[str], limit: int
    ) -> list[dict]:
        """angel_memory：judgment/reasoning 全文匹配 + scope 归属过滤。

        scope 只有 'public' 时人人可见；带 user_id 的 scope 只给本人。
        """
        out: list[dict] = []
        with SqliteReader(self._angel_db) as con:
            rows = con.execute(
                "SELECT id, memory_type, judgment, reasoning, strength, is_active,"
                " memory_scope, created_at, updated_at FROM memory_records"
            ).fetchall()
        for row in rows:
            scope = str(row[6] or "")
            mine = scope == "public" or participant_id in scope
            if not mine:
                continue
            if str(row[5]) != "1":
                continue
            text = "{}\n{}".format(row[2] or "", row[3] or "")
            score = _score(text, tokens)
            if score <= 0:
                continue
            out.append(
                {
                    "source": self.SRC_ANGEL,
                    "kind": "judgment",
                    "text": (row[2] or "").strip(),
                    "detail": (row[3] or "").strip(),
                    "memory_type": row[1] or "",
                    "scope": scope,
                    "strength": row[4] or 0,
                    "score": score,
                    "ts": row[8] or row[7] or 0,
                }
            )
        out.sort(key=lambda x: (-x["score"], -x["strength"]))
        return out[:limit]

    # ---------- self_learning ----------

    def _recall_self(
        self, participant_id: str, tokens: list[str], limit: int
    ) -> list[dict]:
        """self_learning：memories（按 group_id/user_id 归属）+ jargon（全局黑话）。"""
        out: list[dict] = []
        with SqliteReader(self._self_db) as con:
            try:
                rows = con.execute(
                    "SELECT content, importance, memory_type, tags, created_at"
                    " FROM memories WHERE group_id = ? OR user_id = ?",
                    (participant_id, participant_id),
                ).fetchall()
            except sqlite3.Error:
                rows = []
            for content, importance, mtype, tags, created in rows:
                text = str(content or "")
                score = _score(text, tokens)
                if score <= 0:
                    continue
                out.append(
                    {
                        "source": self.SRC_SELF,
                        "kind": "memory",
                        "text": text.strip(),
                        "memory_type": mtype or "",
                        "tags": tags or "",
                        "importance": importance or 0,
                        # 重要度参与打分：importance 0-10 → 最多加 0.3
                        "score": round(min(1.0, score + min(importance or 0, 10) * 0.03), 4),
                        "ts": created or 0,
                    }
                )

            # 黑话/口头禅：全局词典，任何人都能用（不含私人内容）
            try:
                jargon = con.execute(
                    "SELECT term, meaning FROM jargon"
                ).fetchall()
            except sqlite3.Error:
                jargon = []
            for term, meaning in jargon:
                blob = "{} {}".format(term or "", meaning or "")
                score = _score(blob, tokens)
                if score <= 0:
                    continue
                out.append(
                    {
                        "source": self.SRC_SELF,
                        "kind": "jargon",
                        "text": "{} —— {}".format(term or "", meaning or "").strip(),
                        "score": score * 0.9,  # 词典权重略低
                        "ts": 0,
                    }
                )
        out.sort(key=lambda x: (-x["score"], -x["ts"]))
        return out[:limit]

    # ---------- 画像 ----------

    def _profile(self, participant_id: str, platform_id: str) -> dict:
        """身份画像：昵称、好感度、首次/最近活跃。任一来源缺失就留空。"""
        profile: dict[str, Any] = {
            "nicknames": [],
            "affection": None,
            "first_seen": None,
            "last_seen": None,
        }

        if self.enable_angel and self._angel_db.exists():
            try:
                with SqliteReader(self._angel_db) as con:
                    # 不按 platform 过滤：QQ 号本身全局唯一，
                    # 而插件记的 platform 可能是 aiocqhttp，桥接侧则是 default
                    row = con.execute(
                        "SELECT user_names, first_seen_at, last_seen_at FROM user_ledger"
                        " WHERE user_id = ? ORDER BY last_seen_at DESC LIMIT 1",
                        (participant_id,),
                    ).fetchone()
                if row:
                    try:
                        names = json.loads(row[0] or "[]")
                        if isinstance(names, list):
                            profile["nicknames"] = [str(n) for n in names if n]
                    except (TypeError, ValueError):
                        pass
                    profile["first_seen"] = row[1]
                    profile["last_seen"] = row[2]
            except Exception:
                pass

        if self.enable_self and self._self_db.exists():
            try:
                with SqliteReader(self._self_db) as con:
                    row = con.execute(
                        "SELECT affection_level, max_affection FROM user_affections"
                        " WHERE user_id = ? OR group_id = ? LIMIT 1",
                        (participant_id, participant_id),
                    ).fetchone()
                if row:
                    profile["affection"] = {
                        "level": row[0],
                        "max": row[1] or 100,
                    }
            except Exception:
                pass

        return profile

    # ---------- 工具 ----------

    @staticmethod
    def resolve_data_dir(explicit: str = "") -> Path:
        """定位 AstrBot 的 data 目录。

        优先用配置里给的路径；否则按插件自身位置反推
        .../core/data/plugins/<plugin>/core/memory.py → parents[3] = .../core/data
        """
        if explicit:
            try:
                candidate = Path(explicit)
                if candidate.exists():
                    return candidate
            except (TypeError, ValueError):
                pass
        # 向上找第一个含 plugin_data 的祖先目录：不硬编码层级，
        # 插件放在 data/plugins/<name>/ 下几层都能认出来
        try:
            here = Path(__file__).resolve()
            for parent in here.parents:
                if (parent / "plugin_data").is_dir():
                    return parent
        except Exception:
            pass
        return Path.cwd()
