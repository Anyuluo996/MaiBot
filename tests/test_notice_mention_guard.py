"""通知消息跳过提及识别测试：notice 文本含 actor 名，别名子串撞名不得触发提及。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.maisaka.scenes.group import mentions as group_mentions
from src.maisaka.scenes.private import mentions as private_mentions


def make_message(is_notify: bool, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        is_notify=is_notify,
        is_at=False,
        is_mentioned=False,
        processed_plain_text=text,
        message_segment=None,
        message_info=SimpleNamespace(additional_config={}),
        raw_message=None,
    )


@pytest.fixture
def patch_mentions(monkeypatch: pytest.MonkeyPatch) -> None:
    keywords = ["岸宝", "三花"]

    def fake_context(message: SimpleNamespace) -> SimpleNamespace:
        return SimpleNamespace(
            text=message.processed_plain_text,
            platform="qq",
            current_accounts=[],
            keywords=keywords,
        )

    reply_timing = SimpleNamespace(inevitable_at_reply=False, mentioned_bot_reply=True)
    monkeypatch.setattr(
        group_mentions,
        "global_config",
        SimpleNamespace(chat=SimpleNamespace(reply_timing=reply_timing)),
    )
    monkeypatch.setattr(group_mentions, "mention_context", fake_context)
    monkeypatch.setattr(private_mentions, "mention_context", fake_context)


def test_notice_text_with_alias_not_mentioned(patch_mentions: None) -> None:
    notice = make_message(
        True,
        "[事件-群消息表情回应] 三花~｜当前系统活力24.23% 对消息(ID:571081008)表达了 [续标识]",
    )
    assert group_mentions.detect_mentions(notice) == (False, False, 0.0)
    assert private_mentions.detect_mentions(notice) == (False, False, 0.0)


def test_normal_text_with_alias_still_mentioned(patch_mentions: None) -> None:
    normal = make_message(False, "三花今天上线了吗")
    assert group_mentions.detect_mentions(normal) == (True, False, 1.0)
    assert private_mentions.detect_mentions(normal) == (True, False, 0.0)


def test_normal_text_without_alias_not_mentioned(patch_mentions: None) -> None:
    normal = make_message(False, "今天天气不错")
    assert group_mentions.detect_mentions(normal) == (False, False, 0.0)
    assert private_mentions.detect_mentions(normal) == (False, False, 0.0)
