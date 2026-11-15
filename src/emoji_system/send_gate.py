"""表情包语义发送门控：长回复抑制、间隔闸、概率闸与发言计数台账。

只有模型按情绪/画面描述自发配图的语义路径经过本模块；上下文表情引用
（引用别人刚发的表情）是显式接梗动作，不参与门控，始终放行。
"""

from typing import Dict, Optional

import random

from src.common.logger import get_logger
from src.config.config import global_config

logger = get_logger("emoji_send_gate")

# stream_id -> 距上一条实际带表情包发言的 bot 发言条数；进程内台账，重启即复位。
_reply_gap_counter: Dict[str, int] = {}


def record_bot_reply(session_id: str, has_emoji: bool) -> None:
    """每条 bot 发言出口记账；本轮实际发出表情包时清零间隔计数。"""
    if has_emoji:
        _reply_gap_counter[session_id] = 0
        logger.info(f"emoji_gate counter=reset session={session_id}")
        return
    _reply_gap_counter[session_id] = _reply_gap_counter.get(session_id, 0) + 1


def check_semantic_send(session_id: str, reply_style: str, recent_sticker_seen: bool) -> Optional[str]:
    """语义配图发送前判定；返回 None 表示放行，否则返回面向模型的拦截原因。"""
    emoji_config = global_config.emoji
    if reply_style == "长回复":
        logger.info(f"emoji_gate decision=blocked reason=style session={session_id}")
        return "本轮回复为长回复，不附带表情包"
    since_last = _reply_gap_counter.get(session_id, 0)
    min_gap = emoji_config.emoji_min_gap_replies
    if since_last < min_gap:
        logger.info(
            f"emoji_gate decision=blocked reason=gap session={session_id} since_last={since_last} min_gap={min_gap}"
        )
        return f"距上一条带表情包的发言仅隔 {since_last} 条，未达到最小间隔 {min_gap} 条，本轮不附带表情包"
    chance = emoji_config.emoji_chance
    # 对方刚发表情包属于接梗时刻，跳过概率闸直接放行。
    if not recent_sticker_seen and random.random() >= chance:
        logger.info(f"emoji_gate decision=blocked reason=chance session={session_id} chance={chance}")
        return "本轮表情包发送概率未命中，不附带表情包"
    logger.info(
        f"emoji_gate decision=pass session={session_id} chance={chance} "
        f"sticker_bypass={recent_sticker_seen} since_last={since_last}"
    )
    return None


def check_below_similarity_floor(session_id: str, similarity: float, floor: float) -> None:
    """相似度低于地板时记录拦截并抛出解析失败；floor<=0 表示不设下限。"""
    if floor <= 0 or similarity >= floor:
        return
    logger.info(
        f"emoji_gate decision=blocked reason=below_floor session={session_id} "
        f"similarity={similarity:.3f} floor={floor:.3f}"
    )
    raise ValueError(f"表情库中没有语义贴合度足够的表情包（最高相似度 {similarity:.2f}，地板 {floor:.2f}）。")
