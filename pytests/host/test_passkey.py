"""通行密钥：注册、断言登录、管理与限流；认证器行为由测试内伪造（ES256 + none attestation）。"""

from hashlib import sha256
from pathlib import Path
from typing import Dict, Optional

import cbor2
import json
import secrets

from aiohttp import ClientSession, CookieJar
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url
import pytest

from src.host.config import HostConfig

from test_webui import free_port, login, running_host


class FakeAuthenticator:
    """最小可用的 WebAuthn 认证器：ES256 密钥对、none 格式证明与计数递增的断言。"""

    def __init__(self, rp_id: str, origin: str) -> None:
        self.rp_id = rp_id
        self.origin = origin
        self.sign_count = 0
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)
        self.aaguid = bytes(16)

    def _cose_key(self) -> bytes:
        numbers = self.private_key.public_key().public_numbers()
        return cbor2.dumps({
            1: 2, 3: -7, -1: 1,
            -2: numbers.x.to_bytes(32, "big"),
            -3: numbers.y.to_bytes(32, "big"),
        })

    def _client_data(self, type_: str, challenge: str) -> bytes:
        return json.dumps(
            {"type": type_, "challenge": challenge, "origin": self.origin}, separators=(",", ":")
        ).encode("utf-8")

    def _signature(self, auth_data: bytes, client_data: bytes) -> bytes:
        return self.private_key.sign(auth_data + sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))

    def create(self, challenge: str) -> Dict[str, object]:
        # flags 0x45 = UP | UV | AT；attestedCredentialData 紧跟 AAGUID 与凭据 ID。
        auth_data = (
            sha256(self.rp_id.encode("utf-8")).digest()
            + b"\x45"
            + self.sign_count.to_bytes(4, "big")
            + self.aaguid
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + self._cose_key()
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(self._client_data("webauthn.create", challenge)),
                "attestationObject": bytes_to_base64url(attestation),
                "transports": ["internal"],
            },
        }

    def get(self, challenge: str) -> Dict[str, object]:
        self.sign_count += 1
        client_data = self._client_data("webauthn.get", challenge)
        # flags 0x05 = UP | UV
        auth_data = (
            sha256(self.rp_id.encode("utf-8")).digest()
            + b"\x05"
            + self.sign_count.to_bytes(4, "big")
        )
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": {
                "authenticatorData": bytes_to_base64url(auth_data),
                "clientDataJSON": bytes_to_base64url(client_data),
                "signature": bytes_to_base64url(self._signature(auth_data, client_data)),
                "userHandle": None,
            },
        }


async def fetch_options(client: ClientSession, base: str, path: str) -> Dict[str, object]:
    # 注册选项是 POST（会话已建立时非 GET 请求要过同源校验），登录选项是 GET。
    method = client.post if path.endswith("register/options") else client.get
    async with method(f"{base}{path}", headers={"Origin": base}) as response:
        assert response.status == 200
        return await response.json()


async def register_passkey(
    client: ClientSession, base: str, authenticator: FakeAuthenticator, name: Optional[str] = None,
) -> Dict[str, object]:
    options = await fetch_options(client, base, "/api/webui/auth/passkey/register/options")
    payload: Dict[str, object] = {**authenticator.create(str(options["challenge"]))}
    if name is not None:
        payload["name"] = name
    async with client.post(
        f"{base}/api/webui/auth/passkey/register", json=payload, headers={"Origin": base},
    ) as response:
        return await response.json()


async def attempt_login(client: ClientSession, base: str, authenticator: FakeAuthenticator) -> Dict[str, object]:
    options = await fetch_options(client, base, "/api/webui/auth/passkey/options")
    async with client.post(
        f"{base}/api/webui/auth/passkey/login",
        json=authenticator.get(str(options["challenge"])),
        headers={"Origin": base},
    ) as response:
        return await response.json()


@pytest.mark.asyncio
async def test_passkey_register_login_and_manage(tmp_path: Path, monkeypatch) -> None:
    config = HostConfig(hosts=["127.0.0.1"], port=free_port())
    async with running_host(tmp_path, config=config, monkeypatch=monkeypatch) as (webui, bot_id, base):
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
            # 未注册任何通行密钥时，登录页不展示通行密钥入口。
            async with client.get(f"{base}/api/webui/auth/check") as response:
                assert await response.json() == {
                    "authenticated": False, "passkey_available": False,
                }

            await login(client, base, webui)
            authenticator = FakeAuthenticator(rp_id="127.0.0.1", origin=base)
            registered = await register_passkey(client, base, authenticator, name="测试密钥")
            assert registered["success"] is True
            assert registered["credential_id"] == bytes_to_base64url(authenticator.credential_id)
            async with client.get(f"{base}/api/webui/auth/passkeys") as response:
                records = (await response.json())["passkeys"]
            assert len(records) == 1
            assert records[0]["name"] == "测试密钥"
            assert records[0]["transports"] == ["internal"]
            assert "public_key" not in records[0]

            # 注册后，未登录的 check 应提示登录页展示通行密钥按钮。
            async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as visitor:
                async with visitor.get(f"{base}/api/webui/auth/check") as response:
                    assert (await response.json())["passkey_available"] is True

            # 断言登录：新客户端拿到会话 Cookie。
            async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as fresh:
                result = await attempt_login(fresh, base, authenticator)
                assert result["valid"] is True
                async with fresh.get(f"{base}/api/host/session") as response:
                    assert response.status == 200

            # 同一断言重放：挑战一次性，必须失败。
            async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as replay:
                options = await fetch_options(replay, base, "/api/webui/auth/passkey/options")
                credential = authenticator.get(str(options["challenge"]))
                async with replay.post(
                    f"{base}/api/webui/auth/passkey/login", json=credential, headers={"Origin": base},
                ) as response:
                    first = await response.json()
                async with replay.post(
                    f"{base}/api/webui/auth/passkey/login", json=credential, headers={"Origin": base},
                ) as response:
                    replayed = await response.json()
                assert first["valid"] is True and replayed["valid"] is False

            # 重命名与删除。
            async with client.patch(
                f"{base}/api/webui/auth/passkeys/{records[0]['credential_id']}",
                json={"name": "笔记本指纹"}, headers={"Origin": base},
            ) as response:
                assert (await response.json())["success"] is True
            async with client.delete(
                f"{base}/api/webui/auth/passkeys/{records[0]['credential_id']}", headers={"Origin": base},
            ) as response:
                assert (await response.json())["success"] is True
            async with client.delete(
                f"{base}/api/webui/auth/passkeys/{records[0]['credential_id']}", headers={"Origin": base},
            ) as response:
                assert response.status == 404


@pytest.mark.asyncio
async def test_passkey_register_rejects_reused_challenge_and_duplicate(tmp_path: Path, monkeypatch) -> None:
    config = HostConfig(hosts=["127.0.0.1"], port=free_port())
    async with running_host(tmp_path, config=config, monkeypatch=monkeypatch) as (webui, bot_id, base):
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
            await login(client, base, webui)
            authenticator = FakeAuthenticator(rp_id="127.0.0.1", origin=base)
            options = await fetch_options(client, base, "/api/webui/auth/passkey/register/options")
            challenge = str(options["challenge"])
            payload = authenticator.create(challenge)
            payload["name"] = "第一把"
            async with client.post(
                f"{base}/api/webui/auth/passkey/register", json=payload, headers={"Origin": base},
            ) as response:
                assert (await response.json())["success"] is True

            # 同一挑战二次注册必须失败。
            async with client.post(
                f"{base}/api/webui/auth/passkey/register", json=payload, headers={"Origin": base},
            ) as response:
                assert (await response.json())["success"] is False

            # 同一凭据 ID 重新注册必须失败。
            fresh = await fetch_options(client, base, "/api/webui/auth/passkey/register/options")
            duplicate = authenticator.create(str(fresh["challenge"]))
            async with client.post(
                f"{base}/api/webui/auth/passkey/register", json=duplicate, headers={"Origin": base},
            ) as response:
                assert (await response.json())["success"] is False


@pytest.mark.asyncio
async def test_passkey_login_is_rate_limited(tmp_path: Path, monkeypatch) -> None:
    config = HostConfig(hosts=["127.0.0.1"], port=free_port())
    async with running_host(tmp_path, config=config, monkeypatch=monkeypatch) as (webui, bot_id, base):
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as admin:
            await login(admin, base, webui)
            authenticator = FakeAuthenticator(rp_id="127.0.0.1", origin=base)
            assert (await register_passkey(admin, base, authenticator, name="限流密钥"))["success"] is True

        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as attacker:
            # 首次断言有效；从第二次起挑战已被消费，重放计入失败限流。
            options = await fetch_options(attacker, base, "/api/webui/auth/passkey/options")
            credential = authenticator.get(str(options["challenge"]))
            async with attacker.post(
                f"{base}/api/webui/auth/passkey/login", json=credential, headers={"Origin": base},
            ) as response:
                assert (await response.json())["valid"] is True
            for _ in range(5):
                async with attacker.post(
                    f"{base}/api/webui/auth/passkey/login", json=credential, headers={"Origin": base},
                ) as response:
                    assert (await response.json())["valid"] is False
            fresh = await fetch_options(attacker, base, "/api/webui/auth/passkey/options")
            async with attacker.post(
                f"{base}/api/webui/auth/passkey/login",
                json=authenticator.get(str(fresh["challenge"])),
                headers={"Origin": base},
            ) as response:
                assert response.status == 429


@pytest.mark.asyncio
async def test_passkey_unknown_credential_counts_toward_limit(tmp_path: Path, monkeypatch) -> None:
    """换用未知凭据 ID 的失败同样计入限流，不能绕过。"""
    config = HostConfig(hosts=["127.0.0.1"], port=free_port())
    async with running_host(tmp_path, config=config, monkeypatch=monkeypatch) as (webui, bot_id, base):
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as admin:
            await login(admin, base, webui)
            authenticator = FakeAuthenticator(rp_id="127.0.0.1", origin=base)
            assert (await register_passkey(admin, base, authenticator))["success"] is True

        stranger = FakeAuthenticator(rp_id="127.0.0.1", origin=base)
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as attacker:
            for _ in range(5):
                assert (await attempt_login(attacker, base, stranger))["valid"] is False
            fresh = await fetch_options(attacker, base, "/api/webui/auth/passkey/options")
            async with attacker.post(
                f"{base}/api/webui/auth/passkey/login",
                json=stranger.get(str(fresh["challenge"])),
                headers={"Origin": base},
            ) as response:
                assert response.status == 429


@pytest.mark.asyncio
async def test_passkey_login_rejects_mismatched_user_handle(tmp_path: Path, monkeypatch) -> None:
    config = HostConfig(hosts=["127.0.0.1"], port=free_port())
    async with running_host(tmp_path, config=config, monkeypatch=monkeypatch) as (webui, bot_id, base):
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
            await login(client, base, webui)
            authenticator = FakeAuthenticator(rp_id="127.0.0.1", origin=base)
            assert (await register_passkey(client, base, authenticator))["success"] is True

            options = await fetch_options(client, base, "/api/webui/auth/passkey/options")
            credential = authenticator.get(str(options["challenge"]))
            # L3 允许 userHandle 为 None（放行）；乱填别人的句柄必须拒绝。
            credential["response"]["userHandle"] = bytes_to_base64url(b"someone-else")
            async with client.post(
                f"{base}/api/webui/auth/passkey/login", json=credential, headers={"Origin": base},
            ) as response:
                assert (await response.json())["valid"] is False
