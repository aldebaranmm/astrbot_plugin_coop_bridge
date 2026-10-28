"""桥接插件配置。

所有配置项在 _conf_schema.json 中有对应声明；这里负责合并默认值，
让插件在配置缺失（旧安装、手工部署）时也能跑起来。
"""

from __future__ import annotations

DEFAULTS: dict = {
    "enabled": True,
    "bind_pending_ttl": 1800,
    "bind_prompt": (
        "第一次见。我识别到你是 {qq}（这个号码来自 QQ 协议层，伪造不了）。"
        "如果没错，回复「确认」完成绑定；如果不是你，回复「不是」。"
    ),
    "bind_done_text": (
        "绑定完成，{qq}。之后我就认得你了。想解除绑定的话，私聊发「解绑」。"
    ),
    "bind_reject_text": "好的，已取消。下次再聊到的话，重新确认一次就行。",
    "unbind_prompt": (
        "解绑会注销这个身份和它名下的所有设备配对。"
        "确认请回复「确认解绑」，其他任何消息取消。"
    ),
    "unbind_done_text": "已解绑。这个身份的所有绑定信息已清除（同时吊销了 {devices} 台设备）。",
    "pair_code_ttl": 600,
    "pair_code_text": (
        "配对码：{code}\n"
        "{minutes} 分钟内有效，只能用一次。\n"
        "在你那台电脑的 Coopanion 桥接设置里填上它，就能连上我。"
    ),
    "device_list_text": "你名下共 {count} 台设备：\n{lines}\n要吊销某台，发「取消配对 名字」。",
    "device_empty_text": "还没有配对的电脑。发「配对」拿一个码，在电脑上填进去就行。",
    "revoke_need_name_text": "要吊销哪台？发「取消配对 名字」。当前的：{names}",
    "revoke_miss_text": "没找到叫「{name}」的设备，发「我的设备」看看列表。",
    "revoke_done_text": "已吊销「{name}」。这台电脑要重新连，得再配对一次。",
    # ---- M3 主动消息双发 ----
    "proactive_dual_send": True,
    # ---- M4 桥接服务 ----
    "server_enabled": True,
    "server_port": 6190,
    "server_bind": "127.0.0.1",
    "astrbot_data_dir": "",
    "recall_limit": 20,
    "include_group_memory": True,
    "src_livingmemory": True,
    "src_angel_memory": True,
    "src_self_learning": True,
    # ---- M6 对话回传 ----
    "mirror_memory": True,
    # ---- M7 叫不应就搞事 ----
    "stunt_enabled": True,
    "stunt_after": 2,
    "defer_in_game": True,
    "stunt_repeat": True,
    # ---- M8 用户活动 ----
    "activity_enabled": True,
    "activity_digest": True,
    "activity_digest_minutes": 30,
}


class BridgeConfig:
    """配置门面：属性访问，缺失回退默认值。"""

    def __init__(self, config=None):
        raw = dict(DEFAULTS)
        if config:
            try:
                items = config.items() if hasattr(config, "items") else dict(config).items()
                for key, value in items:
                    if key in raw and value is not None:
                        raw[key] = value
            except Exception:
                pass
        self._raw = raw

    @property
    def enabled(self) -> bool:
        return bool(self._raw["enabled"])

    @property
    def bind_pending_ttl(self) -> int:
        try:
            return max(60, int(self._raw["bind_pending_ttl"]))
        except (TypeError, ValueError):
            return 1800

    @property
    def bind_prompt(self) -> str:
        return str(self._raw["bind_prompt"])

    @property
    def bind_done_text(self) -> str:
        return str(self._raw["bind_done_text"])

    @property
    def bind_reject_text(self) -> str:
        return str(self._raw["bind_reject_text"])

    @property
    def unbind_prompt(self) -> str:
        return str(self._raw["unbind_prompt"])

    @property
    def unbind_done_text(self) -> str:
        return str(self._raw["unbind_done_text"])

    @property
    def pair_code_ttl(self) -> int:
        try:
            return max(60, int(self._raw["pair_code_ttl"]))
        except (TypeError, ValueError):
            return 600

    @property
    def pair_code_text(self) -> str:
        return str(self._raw["pair_code_text"])

    @property
    def device_list_text(self) -> str:
        return str(self._raw["device_list_text"])

    @property
    def device_empty_text(self) -> str:
        return str(self._raw["device_empty_text"])

    @property
    def revoke_need_name_text(self) -> str:
        return str(self._raw["revoke_need_name_text"])

    @property
    def revoke_miss_text(self) -> str:
        return str(self._raw["revoke_miss_text"])

    @property
    def revoke_done_text(self) -> str:
        return str(self._raw["revoke_done_text"])

    # ---------- M3 ----------

    @property
    def proactive_dual_send(self) -> bool:
        return bool(self._raw["proactive_dual_send"])

    # ---------- M4 ----------

    @property
    def server_enabled(self) -> bool:
        return bool(self._raw["server_enabled"])

    @property
    def server_port(self) -> int:
        try:
            return max(1024, min(65535, int(self._raw["server_port"])))
        except (TypeError, ValueError):
            return 6190

    @property
    def server_bind(self) -> str:
        return str(self._raw["server_bind"] or "127.0.0.1")

    @property
    def astrbot_data_dir(self) -> str:
        return str(self._raw["astrbot_data_dir"] or "")

    @property
    def recall_limit(self) -> int:
        try:
            return max(1, min(50, int(self._raw["recall_limit"])))
        except (TypeError, ValueError):
            return 20

    @property
    def stunt_enabled(self) -> bool:
        """连着叫几次没应声时，是否让桌宠搞事（按 Win 键 / 切窗口 / 凑到鼠标边）。"""
        return bool(self._raw["stunt_enabled"])

    @property
    def stunt_after(self) -> int:
        """未回复几次后开始搞事；至少 2 次，避免第一次没回就动手。"""
        try:
            return max(2, min(10, int(self._raw["stunt_after"])))
        except (TypeError, ValueError):
            return 2

    @property
    def activity_enabled(self) -> bool:
        """回话前是否把「主人现在在干什么」注入上下文。"""
        return bool(self._raw["activity_enabled"])

    @property
    def activity_digest(self) -> bool:
        """是否定时把活动压成一句写进 livingmemory。"""
        return bool(self._raw["activity_digest"])

    @property
    def activity_digest_minutes(self) -> int:
        """汇总间隔（分钟），限制在 10~180。"""
        try:
            return max(10, min(180, int(self._raw["activity_digest_minutes"])))
        except (TypeError, ValueError):
            return 30

    @property
    def stunt_repeat(self) -> bool:
        """没回复之前是否反复走过来催（只走路，不再重复说话）。"""
        return bool(self._raw["stunt_repeat"])

    @property
    def defer_in_game(self) -> bool:
        """检测到游戏在跑时，先把话攒着，等游戏关掉再补发。"""
        return bool(self._raw["defer_in_game"])

    @property
    def mirror_memory(self) -> bool:
        """对话回传时是否同时镜像进 livingmemory（没装就自动跳过）。"""
        return bool(self._raw["mirror_memory"])

    @property
    def include_group_memory(self) -> bool:
        return bool(self._raw["include_group_memory"])

    @property
    def src_livingmemory(self) -> bool:
        return bool(self._raw["src_livingmemory"])

    @property
    def src_angel_memory(self) -> bool:
        return bool(self._raw["src_angel_memory"])

    @property
    def src_self_learning(self) -> bool:
        return bool(self._raw["src_self_learning"])
