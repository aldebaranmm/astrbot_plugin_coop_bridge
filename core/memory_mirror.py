"""把 Coopanion 里的对话镜像进 livingmemory 记忆插件（M6 补充）。

背景：livingmemory 只靠实时钩子（on_llm_request / after_message_sent）采集，
不会扫描会话库；所以「写进会话历史」只让模型本轮看得见，它并不吸收。
要让记忆真正沉淀，得调用它自己的写入接口：

    livingmemory 插件实例 .initializer .memory_engine .add_memory(content, session_id=umo, ...)

这里按「插件没装 / 未初始化 / 写入报错」三种情况全部软失败——镜像记忆是加分项，
绝不能因为它让对话回传整体失败。
"""

from __future__ import annotations

from typing import Any, Optional

# livingmemory 的插件目录名（AstrBot 用它做 star name）
LIVINGMEMORY_STAR = "astrbot_plugin_livingmemory"

# 一句话记忆的模板：检索时既能看到主人原话，也能看到桌宠的回应
MEMORY_TEMPLATE = "主人和桌宠（Coopanion）聊天：主人说「{user}」，桌宠回答「{reply}」"


def identity_metadata(umo: str, topics: list[str] | None = None) -> dict:
    """把「这条记忆属于谁」写进 metadata。

    livingmemory 的记忆图谱靠 `participant_identities` 建 `Person <QQ>` 节点，
    再和 fact 连成 `mentioned_in` 边；不传这个字段，记忆在图谱里就是孤立节点
    （表现为「连接度 0」，也看不出属于谁）。

    identity_key 必须和 AstrBot 自己处理 QQ 群消息时一致——即 `aiocqhttp:<QQ号>`。
    桌宠主人的 QQ 号就是 QQ 机器人（aiocqhttp 平台）的账号，所以这里把
    `default:<QQ号>` 归一成 `aiocqhttp:<QQ号>`。否则同一个 QQ 号会被拆成
    「QQ 号」和「QQ 昵称」两个互不连线的 person 节点。
    """
    # umo 是三段：platform:FriendMessage:QQ（也兼容两段写法）
    parts = [piece for piece in (umo or "").split(":") if piece]
    if len(parts) >= 3:
        platform, participant = parts[0], parts[-1]
    elif len(parts) == 2:
        platform, participant = parts
    else:
        return {}
    meta: dict = {
        "participants": [participant],
        "participant_identities": [
            {
                "identity_key": f"aiocqhttp:{participant}",
                "display_name": participant,
                "sender_id": participant,
                "platform": platform or "default",
                "aliases": ["主人"],
                "is_bot": False,
            }
        ],
    }
    if topics:
        meta["topics"] = topics
    return meta


def find_memory_engine(context: Any) -> Optional[Any]:
    """从 AstrBot 的 context 找到 livingmemory 的记忆引擎；任何一步缺失都返回 None。"""
    getter = getattr(context, "get_registered_star", None)
    if getter is None:
        return None
    try:
        meta = getter(LIVINGMEMORY_STAR)
    except Exception:
        return None
    plugin = getattr(meta, "star_cls", None) if meta is not None else None
    if plugin is None:
        return None
    initializer = getattr(plugin, "initializer", None)
    engine = getattr(initializer, "memory_engine", None)
    if engine is None or not hasattr(engine, "add_memory"):
        return None
    return engine


async def mirror_note(
    context: Any,
    umo: str,
    content: str,
    kind: str = "note",
    importance: float = 0.3,
    logger: Any = None,
    topics: list[str] | None = None,
) -> bool:
    """把一句独立的话写进 livingmemory（活动汇总等非对话内容用）。

    importance 默认 0.3，低于对话的 0.5：这类条目是背景信息，
    检索时不该压过真正聊过的内容。任何失败都返回 False 且不抛。
    """
    text = (content or "").strip()
    if not text or not umo:
        return False

    engine = find_memory_engine(context)
    if engine is None:
        return False

    default_topics = ["电脑活动"] if kind == "activity_digest" else None
    try:
        await engine.add_memory(
            text,
            session_id=umo,
            importance=importance,
            metadata={
                "source": "coopanion",
                "kind": kind,
                **identity_metadata(
                    umo,
                    topics=topics if topics is not None else default_topics,
                ),
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 记忆是加分项，绝不影响主流程
        if logger is not None:
            try:
                logger.warning("[coop_bridge] 写入记忆失败（%s）: %s", kind, exc)
            except Exception:
                pass
        return False


async def mirror_chat(
    context: Any,
    umo: str,
    user_text: str,
    reply_text: str,
    logger: Any = None,
) -> bool:
    """把一轮对话写进 livingmemory。成功返回 True，任何失败都返回 False 且不抛。"""
    user_text = (user_text or "").strip()
    reply_text = (reply_text or "").strip()
    if not user_text or not reply_text:
        return False

    engine = find_memory_engine(context)
    if engine is None:
        return False

    content = MEMORY_TEMPLATE.format(user=user_text[:500], reply=reply_text[:500])
    try:
        await engine.add_memory(
            content,
            session_id=umo,
            importance=0.5,
            metadata={
                "source": "coopanion",
                "kind": "pet_chat",
                **identity_metadata(umo, topics=["桌宠聊天"]),
            },
            source_messages=[
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": reply_text},
            ],
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 记忆是加分项，绝不影响主流程
        if logger is not None:
            try:
                logger.warning("[coop_bridge] 镜像到 livingmemory 失败（会话历史已写入）: %s", exc)
            except Exception:
                pass
        return False
