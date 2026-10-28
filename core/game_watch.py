"""检测是否正在玩 Steam 游戏（M7）。

用途只有一个：判断「现在适不适合让桌宠来打扰」。游戏在跑就把话攒着，
等关掉之后再补发——既不会在游戏里弹气泡，也不会往游戏里注入任何输入。

识别方式：进程可执行文件路径落在 `steamapps\\common\\` 下。
不是 Windows、PowerShell 不可用、超时或输出异常，一律当作「没在玩游戏」（宁可打扰，也不憋着不说）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Optional

STEAM_MARKER = "steamapps\\common"

_PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$games = @()
Get-Process | Where-Object { $_.Path -and ($_.Path -like '*\steamapps\common\*') } | ForEach-Object {
    $games += [pscustomobject]@{ id = $_.Id; name = $_.ProcessName; path = $_.Path }
}
[pscustomobject]@{ games = @($games) } | ConvertTo-Json -Compress -Depth 4
"""


def _run_powershell(script: str, timeout: float = 8.0) -> Optional[str]:
    exe = None
    for name in ("powershell.exe", "powershell", "pwsh"):
        try:
            found = shutil.which(name)
            if found:
                exe = found
                break
        except Exception:
            continue
    if exe is None:
        return None
    try:
        completed = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            capture_output=True,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()
    except Exception:
        return None


def parse_status(raw: Optional[str]) -> dict[str, Any]:
    """把 PowerShell 的 JSON 解析成 {games: [...]}；坏数据一律当空。"""
    if not raw:
        return {"games": []}
    try:
        data = json.loads(raw)
    except Exception:
        return {"games": []}
    games = data.get("games") if isinstance(data, dict) else None
    if isinstance(games, dict):
        games = [games]
    if not isinstance(games, list):
        return {"games": []}
    return {"games": [g for g in games if isinstance(g, dict)]}


def detect() -> dict[str, Any]:
    """返回 {running: bool, game: str}。"""
    if os.name != "nt":
        return {"running": False, "game": ""}
    games = parse_status(_run_powershell(_PS_SCRIPT))["games"]
    if not games:
        return {"running": False, "game": ""}
    return {"running": True, "game": str(games[0].get("name") or "")}
