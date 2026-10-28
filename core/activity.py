"""用户活动记录（M8）。

Coopanion 定时上报「主人现在在干什么」，这里存最近若干条，并按身份隔离。
用途有两个：
  1. AstrBot 在回话前把最新一条注入上下文（`on_llm_request`），于是它能自然提到「你在写代码」；
  2. 桌宠通过 `GET /bridge/activity` 自己查。

隐私边界（重要）：
  - 进程、窗口标题、空闲时长由扩展本地采集后上报，不涉及文件内容；
  - 文件夹只是「文件名 + 修改时间 + 大小」，而且扩展侧只有在用户显式打开
    `允许读取文件夹` 并填了路径时才会读，关着就完全不碰文件系统；
  - 这里不做任何外发，只在本机内存 + 一个 JSON 文件里。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

# 每个身份最多留多少条历史（够还原「刚才在干嘛」就行）
KEEP_PER_IDENTITY = 20


def _ago(seconds: float) -> str:
    """把秒数说成人话。"""
    value = int(max(0, seconds))
    if value < 60:
        return f"{value} 秒前"
    if value < 3600:
        return f"{value // 60} 分钟前"
    return f"{value // 3600} 小时前"


class ActivityStore:
    def __init__(self, path: Optional[Path] = None, keep: int = KEEP_PER_IDENTITY):
        self.path = Path(path) if path else None
        self.keep = max(1, keep)
        self._data: dict[str, list[dict]] = {}
        self._load()

    # ---------- 读写 ----------

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(value, list):
                    self._data[str(key)] = [v for v in value if isinstance(v, dict)][-self.keep :]

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    def record(self, identity_id: str, payload: dict) -> dict:
        """记一条活动。只挑需要的字段，防止上游塞进来一堆没用的东西。"""
        if not identity_id:
            raise ValueError("缺少身份")
        window = payload.get("window") if isinstance(payload.get("window"), dict) else {}
        entry = {
            "ts": time.time(),
            "window": {
                "title": str(window.get("title") or "")[:200],
                "process": str(window.get("process") or "")[:80],
                "fullscreen": bool(window.get("fullscreen")),
            },
            "idle_seconds": int(payload.get("idle_seconds") or 0),
            "folders": self._clean_folders(payload.get("folders")),
        }
        bucket = self._data.setdefault(identity_id, [])
        bucket.append(entry)
        del bucket[: max(0, len(bucket) - self.keep)]
        self._save()
        return entry

    @staticmethod
    def _clean_folders(raw: Any) -> list[dict]:
        """只留 路径 + 文件名/时间/大小；没有路径的直接丢。"""
        if not isinstance(raw, list):
            return []
        result: list[dict] = []
        for folder in raw[:5]:
            if not isinstance(folder, dict):
                continue
            path = str(folder.get("path") or "").strip()
            if not path:
                continue
            files = []
            for item in (folder.get("files") or [])[:20]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                files.append(
                    {
                        "name": name[:120],
                        "mtime": float(item.get("mtime") or 0),
                        "size": int(item.get("size") or 0),
                    }
                )
            result.append({"path": path[:260], "files": files})
        return result

    # ---------- 查询 ----------

    def latest(self, identity_id: str) -> Optional[dict]:
        bucket = self._data.get(identity_id) or []
        return bucket[-1] if bucket else None

    def history(self, identity_id: str) -> list[dict]:
        return list(self._data.get(identity_id) or [])

    def identities(self) -> list[str]:
        """所有有活动记录的身份（定时汇总要逐个过一遍）。"""
        return [key for key, bucket in self._data.items() if bucket]

    def describe(self, identity_id: str) -> str:
        """给模型看的一句话；没数据返回空串。"""
        entry = self.latest(identity_id)
        if not entry:
            return ""
        window = entry.get("window") or {}
        title = (window.get("title") or "").strip()
        process = (window.get("process") or "").strip()
        idle = int(entry.get("idle_seconds") or 0)
        age = time.time() - float(entry.get("ts") or 0)

        parts: list[str] = []
        if title or process:
            label = title or process
            parts.append(f"主人现在前台是「{label}」" + (f"（{process}）" if process and process != label else ""))
        else:
            parts.append("主人现在没有明显的前台窗口")
        if idle >= 300:
            parts.append(f"已经 {idle // 60} 分钟没动键鼠，可能离开了")
        elif idle >= 60:
            parts.append(f"约 {idle // 60} 分钟没动键鼠")
        if age > 180:
            parts.append(f"这条状态是 {_ago(age)}的，可能已经变了")

        folders = entry.get("folders") or []
        if folders:
            bits = []
            for folder in folders[:2]:
                files = folder.get("files") or []
                if not files:
                    continue
                latest = max(files, key=lambda f: float(f.get("mtime") or 0))
                bits.append(f"{folder.get('path')} 里最近动过「{latest.get('name')}」")
            if bits:
                parts.append("；".join(bits))

        return "（本地采集，非用户原话）" + "，".join(parts) + "。"
