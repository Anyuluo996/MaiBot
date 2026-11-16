"""attach_at 正文重复称呼剥离测试：@ 前缀与称呼并存时不再重复显示昵称。"""

from __future__ import annotations

from src.common.data_models.message_component_data_model import AtComponent
from src.maisaka.builtin_tool.context import strip_duplicated_at_vocative


def make_at(nickname: str, cardname: str | None = None) -> AtComponent:
    return AtComponent("251246627", target_user_nickname=nickname, target_user_cardname=cardname)


def test_vocative_with_punctuation_stripped() -> None:
    text = "黄小炮，你的账号还没绑定，暂时查不到体力"
    assert strip_duplicated_at_vocative(text, [make_at("黄小炮")]) == "你的账号还没绑定，暂时查不到体力"


def test_vocative_with_space_stripped() -> None:
    assert strip_duplicated_at_vocative("黄小炮 首充不接", [make_at("黄小炮")]) == "首充不接"


def test_at_marked_vocative_stripped() -> None:
    assert strip_duplicated_at_vocative("@黄小炮，你先绑定", [make_at("黄小炮")]) == "你先绑定"


def test_markup_form_stripped() -> None:
    assert strip_duplicated_at_vocative("@<黄小炮:251246627>你先绑定", [make_at("黄小炮")]) == "你先绑定"


def test_name_inside_word_kept() -> None:
    text = "黄小炮号很好用啊"
    assert strip_duplicated_at_vocative(text, [make_at("黄小炮")]) == text


def test_unrelated_nickname_kept() -> None:
    text = "炮总别惦记我钱包"
    assert strip_duplicated_at_vocative(text, [make_at("黄小炮")]) == text


def test_whole_text_is_vocative_kept() -> None:
    text = "黄小炮，"
    assert strip_duplicated_at_vocative(text, [make_at("黄小炮")]) == text


def test_cardname_takes_precedence() -> None:
    assert strip_duplicated_at_vocative("安总，来一下", [make_at("黄小炮", cardname="安总")]) == "来一下"


def test_no_mentions_returns_text() -> None:
    text = "黄小炮，在吗"
    assert strip_duplicated_at_vocative(text, []) == text
