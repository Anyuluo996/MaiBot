from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

import src.emoji_system.emoji_manager as emoji_manager_module
from src.common.data_models.image_data_model import MaiEmoji


def _make_emoji(tmp_path: Path) -> MaiEmoji:
    image_path = tmp_path / "candidate.png"
    Image.new("RGB", (4, 4), color=(255, 0, 0)).save(image_path)
    return MaiEmoji(full_path=image_path)


class _FakeSession:
    """模拟数据库会话：查询一律返回空结果。"""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def exec(self, statement):
        return SimpleNamespace(first=lambda: None)


def test_split_non_emoji_description_variants():
    parse = emoji_manager_module._split_non_emoji_description

    assert parse("NOT_EMOJI：游戏结算界面") == "游戏结算界面"
    assert parse("not emoji") == ""
    assert parse("Not_Emoji: 收款二维码") == "收款二维码"
    assert parse("NOT_EMOTION") is None
    assert parse("开心,大笑") is None
    assert parse("") is None


@pytest.mark.asyncio
async def test_build_description_flags_non_emoji_without_hook(monkeypatch, tmp_path):
    manager = emoji_manager_module.emoji_manager
    target = _make_emoji(tmp_path)
    monkeypatch.setattr(emoji_manager_module, "_is_vlm_task_configured", lambda: True)
    monkeypatch.setattr(manager, "analyze_emoji_image", AsyncMock(return_value="NOT_EMOJI：游戏结算界面"))
    hook_manager = Mock()
    monkeypatch.setattr(emoji_manager_module, "_get_runtime_manager", lambda: hook_manager)

    status, result = await manager.build_emoji_description(target)

    assert status == "not_emoji"
    assert result.description == "游戏结算界面"
    hook_manager.assert_not_called()


@pytest.mark.asyncio
async def test_build_description_ok_path_returns_tags(monkeypatch, tmp_path):
    manager = emoji_manager_module.emoji_manager
    target = _make_emoji(tmp_path)
    monkeypatch.setattr(emoji_manager_module, "_is_vlm_task_configured", lambda: True)
    monkeypatch.setattr(manager, "analyze_emoji_image", AsyncMock(return_value="开心,大笑"))
    fake_manager = SimpleNamespace(invoke_hook=AsyncMock(return_value=SimpleNamespace(aborted=False, kwargs={})))
    monkeypatch.setattr(emoji_manager_module, "_get_runtime_manager", lambda: fake_manager)

    status, result = await manager.build_emoji_description(target)

    assert status == "ok"
    assert result.description == "开心,大笑"
    assert result.emotion == ["开心", "大笑"]
    fake_manager.invoke_hook.assert_awaited_once()


@pytest.mark.asyncio
async def test_build_and_cache_discards_non_emoji(monkeypatch, tmp_path):
    manager = emoji_manager_module.emoji_manager
    target = _make_emoji(tmp_path)
    monkeypatch.setattr(manager, "ensure_emoji_saved", AsyncMock(return_value=target))
    monkeypatch.setattr(emoji_manager_module, "_is_vlm_task_configured", lambda: True)
    monkeypatch.setattr(manager, "analyze_emoji_image", AsyncMock(return_value="NOT_EMOJI"))
    discard = Mock()
    monkeypatch.setattr(manager, "_discard_non_emoji_file", discard)

    result = await manager._build_and_cache_emoji_description("hash-x", b"png-bytes")

    assert result == ("非表情包图片", ["非表情包图片"])
    discard.assert_called_once_with(target)


@pytest.mark.asyncio
async def test_register_by_filename_skips_non_emoji(monkeypatch, tmp_path):
    manager = emoji_manager_module.emoji_manager
    image_path = tmp_path / "screenshot.png"
    Image.new("RGB", (4, 4), color=(0, 255, 0)).save(image_path)
    monkeypatch.setattr(emoji_manager_module, "get_db_session_manual", lambda: _FakeSession())

    async def fake_build(target_emoji, *, session_id=""):
        target_emoji.description = "收款二维码"
        return "not_emoji", target_emoji

    monkeypatch.setattr(manager, "build_emoji_description", fake_build)
    discard = Mock()
    mark_processed = Mock()
    monkeypatch.setattr(manager, "_discard_non_emoji_file", discard)
    monkeypatch.setattr(manager, "_mark_emoji_vlm_processed", mark_processed)

    status = await manager.register_emoji_by_filename(image_path)

    assert status == "skipped"
    discard.assert_called_once()
    mark_processed.assert_not_called()
