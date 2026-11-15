"""表情包语义发送门控测试：长回复抑制 / 间隔闸 / 概率闸 / 接梗放行 / 计数台账。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.emoji_system import send_gate


class FakeEmojiConfig:
    def __init__(self, chance: float = 1.0, min_gap: int = 1) -> None:
        self.emoji_chance = chance
        self.emoji_min_gap_replies = min_gap


@pytest.fixture
def emoji_config(monkeypatch: pytest.MonkeyPatch) -> FakeEmojiConfig:
    config = FakeEmojiConfig()
    monkeypatch.setattr(send_gate, "global_config", SimpleNamespace(emoji=config))
    send_gate._reply_gap_counter.clear()
    return config


def test_long_reply_always_blocked(emoji_config: FakeEmojiConfig) -> None:
    emoji_config.emoji_chance = 1.0
    emoji_config.emoji_min_gap_replies = 0
    reason = send_gate.check_semantic_send("s1", "长回复", False)
    assert reason is not None
    assert "长回复" in reason
    assert send_gate.check_semantic_send("s1", "正常回复", False) is None


def test_gap_gate_blocks_consecutive_replies(emoji_config: FakeEmojiConfig) -> None:
    emoji_config.emoji_chance = 1.0
    send_gate.record_bot_reply("s1", has_emoji=True)
    reason = send_gate.check_semantic_send("s1", "正常回复", False)
    assert reason is not None
    assert "最小间隔" in reason


def test_gap_gate_reopens_after_plain_replies(emoji_config: FakeEmojiConfig) -> None:
    emoji_config.emoji_chance = 1.0
    emoji_config.emoji_min_gap_replies = 2
    send_gate.record_bot_reply("s1", has_emoji=True)
    send_gate.record_bot_reply("s1", has_emoji=False)
    assert send_gate.check_semantic_send("s1", "正常回复", False) is not None
    send_gate.record_bot_reply("s1", has_emoji=False)
    assert send_gate.check_semantic_send("s1", "正常回复", False) is None


def test_gap_gate_disabled_at_zero(emoji_config: FakeEmojiConfig) -> None:
    emoji_config.emoji_chance = 1.0
    emoji_config.emoji_min_gap_replies = 0
    send_gate.record_bot_reply("s1", has_emoji=True)
    assert send_gate.check_semantic_send("s1", "正常回复", False) is None


def test_chance_gate_blocks_and_sticker_bypasses(
    emoji_config: FakeEmojiConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    emoji_config.emoji_chance = 0.0
    send_gate.record_bot_reply("s1", has_emoji=False)
    monkeypatch.setattr(send_gate.random, "random", lambda: 0.99)
    assert send_gate.check_semantic_send("s1", "正常回复", False) is not None
    # 接梗时刻跳过概率闸。
    assert send_gate.check_semantic_send("s1", "正常回复", True) is None


def test_chance_gate_passes_on_low_roll(
    emoji_config: FakeEmojiConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    emoji_config.emoji_chance = 0.3
    send_gate.record_bot_reply("s1", has_emoji=False)
    monkeypatch.setattr(send_gate.random, "random", lambda: 0.2)
    assert send_gate.check_semantic_send("s1", "正常回复", False) is None


def test_counter_is_per_session(emoji_config: FakeEmojiConfig) -> None:
    emoji_config.emoji_chance = 1.0
    send_gate.record_bot_reply("s1", has_emoji=True)
    send_gate.record_bot_reply("s2", has_emoji=False)
    assert send_gate.check_semantic_send("s1", "正常回复", False) is not None
    assert send_gate.check_semantic_send("s2", "正常回复", False) is None


def test_counter_increments_on_plain_replies(emoji_config: FakeEmojiConfig) -> None:
    emoji_config.emoji_chance = 1.0
    send_gate.record_bot_reply("s1", has_emoji=False)
    send_gate.record_bot_reply("s1", has_emoji=False)
    assert send_gate._reply_gap_counter["s1"] == 2
    send_gate.record_bot_reply("s1", has_emoji=True)
    assert send_gate._reply_gap_counter["s1"] == 0


def test_below_floor_raises(emoji_config: FakeEmojiConfig) -> None:
    with pytest.raises(ValueError):
        send_gate.check_below_similarity_floor("s1", 0.21, 0.30)
    # 高于地板与 floor=0（不设下限）都放行。
    send_gate.check_below_similarity_floor("s1", 0.50, 0.30)
    send_gate.check_below_similarity_floor("s1", 0.10, 0.0)
