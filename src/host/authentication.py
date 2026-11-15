"""宿主持有的 WebUI 登录凭据；旧用户首次进入宿主时由布局判定复制原密码。"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
from webauthn.helpers import bytes_to_base64url, generate_user_handle

import json
import os
import secrets
import threading
import time

MAX_PASSKEYS = 16


class HostCredentials:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        if not self.path.exists():
            self._save({"access_token": secrets.token_hex(32), "token_source": "temporary", "first_setup_completed": False})
            return
        self._data = self._read(self.path)
        if self.token_source == "temporary":
            # 临时密码只在本次宿主运行内有效，重启后重新生成并打印。
            self._save({**self._data, "access_token": secrets.token_hex(32)})

    @staticmethod
    def _read(path: Path) -> Dict[str, Any]:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"]:
            raise ValueError(f"WebUI 凭据文件无效，拒绝覆盖：{path}")
        if data.get("token_source") not in {"temporary", "configured"}:
            token = data["access_token"]
            data["token_source"] = "temporary" if len(token) == 64 and all(c in "0123456789abcdef" for c in token) else "configured"
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)
            self._data = data
        finally:
            temporary.unlink(missing_ok=True)

    @property
    def token(self) -> str:
        return self._data["access_token"]

    @property
    def token_source(self) -> str:
        return self._data["token_source"]

    def verify(self, token: str) -> bool:
        return secrets.compare_digest(self.token.encode("utf-8"), token.encode("utf-8"))

    def setup_status(self) -> Dict[str, Any]:
        return {
            "is_first_setup": not self._data.get("first_setup_completed", False),
            "token_source": self.token_source,
            "requires_custom_token": self.token_source == "temporary",
        }

    def update(self, new_token: str) -> None:
        if len(new_token) < 10 or not any("A" <= c <= "Z" for c in new_token) or not any("a" <= c <= "z" for c in new_token):
            raise ValueError("Token 至少 10 位，且包含大写字母、小写字母和特殊符号")
        if not any(c in "!@#$%^&*()_+-=[]{}|;:,.<>?/" for c in new_token):
            raise ValueError("Token 必须包含特殊符号")
        with self._lock:
            self._save({**self._data, "access_token": new_token, "token_source": "configured"})

    def regenerate(self) -> str:
        token = secrets.token_hex(32)
        with self._lock:
            self._save({**self._data, "access_token": token, "token_source": "configured"})
        return token

    def complete_setup(self) -> None:
        if self.token_source == "temporary":
            raise ValueError("请先设置固定 WebUI 密码")
        with self._lock:
            self._save({**self._data, "first_setup_completed": True})

    def reset_setup(self, reset_password: bool) -> None:
        with self._lock:
            data = {**self._data, "first_setup_completed": False}
            if reset_password:
                # 打回首次配置态是失陷恢复路径：密码换成新临时值的同时必须作废全部通行密钥，
                # 否则此前（含被恶意）注册的密钥在全新凭据下仍可直接登录。
                data.update(access_token=secrets.token_hex(32), token_source="temporary", passkeys=[])
            self._save(data)

    def webauthn_user_handle(self) -> str:
        """WebAuthn 用户句柄：首次调用生成并固定，后续注册都归属同一用户。"""
        with self._lock:
            handle = self._data.get("webauthn_user_handle")
            if isinstance(handle, str) and handle:
                return handle
            handle = bytes_to_base64url(generate_user_handle())
            self._save({**self._data, "webauthn_user_handle": handle})
            return handle

    def passkeys(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(record) for record in self._data.get("passkeys", [])]

    def passkey(self, credential_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for record in self._data.get("passkeys", []):
                if record.get("credential_id") == credential_id:
                    return dict(record)
            return None

    def put_passkey(self, record: Dict[str, Any]) -> None:
        with self._lock:
            remaining = [
                entry for entry in self._data.get("passkeys", [])
                if entry.get("credential_id") != record["credential_id"]
            ]
            remaining.append(dict(record))
            self._save({**self._data, "passkeys": remaining})

    def drop_passkey(self, credential_id: str) -> bool:
        with self._lock:
            records = self._data.get("passkeys", [])
            remaining = [entry for entry in records if entry.get("credential_id") != credential_id]
            if len(remaining) == len(records):
                return False
            self._save({**self._data, "passkeys": remaining})
            return True

    def touch_passkey(self, credential_id: str, *, sign_count: int, backed_up: bool) -> bool:
        """登录成功后更新签名计数与备份标记；凭据已被并发删除时返回 False 且不复活记录。"""
        with self._lock:
            records = self._data.get("passkeys", [])
            updated: List[Dict[str, Any]] = []
            changed = False
            for record in records:
                if record.get("credential_id") == credential_id:
                    record = {**record, "sign_count": sign_count, "backed_up": backed_up, "last_used_at": time.time()}
                    changed = True
                updated.append(record)
            if changed:
                self._save({**self._data, "passkeys": updated})
            return changed

    def update_passkey_name(self, credential_id: str, name: str) -> bool:
        """重命名走加锁的存在性写入，避免与并发删除交错时复活已删凭据。"""
        with self._lock:
            records = self._data.get("passkeys", [])
            updated: List[Dict[str, Any]] = []
            changed = False
            for record in records:
                if record.get("credential_id") == credential_id:
                    record = {**record, "name": name}
                    changed = True
                updated.append(record)
            if changed:
                self._save({**self._data, "passkeys": updated})
            return changed


class LoginAttempts:
    """认证失败限流；不信任浏览器传来的转发 IP。"""

    def __init__(self) -> None:
        self._failures: Dict[str, Tuple[int, float]] = {}

    def blocked(self, address: str) -> bool:
        now = time.monotonic()
        self._failures = {key: entry for key, entry in self._failures.items() if entry[1] > now}
        entry = self._failures.get(address)
        return entry is not None and entry[0] >= 5

    def record(self, address: str, success: bool) -> None:
        if success:
            self._failures.pop(address, None)
            return
        if len(self._failures) >= 2048 and address not in self._failures:
            raise ValueError("登录请求过多，请稍后再试")
        count, deadline = self._failures.get(address, (0, time.monotonic() + 600))
        self._failures[address] = (count + 1, deadline)
