import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  deletePasskey,
  listPasskeys,
  loginWithPasskey,
  passkeyLoginAvailable,
  registerPasskey,
  renamePasskey,
  webAuthnAvailable,
} from '../webauthn'

const fetchMock = vi.fn()

function jsonResponse(body: unknown, ok = true, status = 200) {
  return { ok, status, text: async () => JSON.stringify(body) }
}

function utf8Buffer(text: string): ArrayBuffer {
  return new TextEncoder().encode(text).buffer as ArrayBuffer
}

function bytesToBase64url(bytes: Uint8Array): string {
  let binary = ''
  for (let index = 0; index < bytes.length; index += 1) {
    binary += String.fromCharCode(bytes[index])
  }
  return window.btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function installCredentialMock(create?: ReturnType<typeof vi.fn>, get?: ReturnType<typeof vi.fn>) {
  Object.defineProperty(window.navigator, 'credentials', {
    configurable: true,
    value: { create, get },
  })
  Object.defineProperty(window, 'PublicKeyCredential', {
    configurable: true,
    value: function PublicKeyCredential() {},
  })
}

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  // 清掉测试注入的 WebAuthn 桩，避免影响其它用例。
  Reflect.deleteProperty(window.navigator, 'credentials')
  Reflect.deleteProperty(window, 'PublicKeyCredential')
  vi.restoreAllMocks()
})

describe('webauthn 能力探测', () => {
  it('jsdom 无 WebAuthn 时 webAuthnAvailable 为 false 且不发探测请求', async () => {
    expect(webAuthnAvailable()).toBe(false)
    await expect(passkeyLoginAvailable()).resolves.toBe(false)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('宿主已注册通行密钥且浏览器支持时展示入口', async () => {
    installCredentialMock(vi.fn(), vi.fn())
    fetchMock.mockResolvedValue(jsonResponse({ authenticated: false, passkey_available: true }))
    await expect(passkeyLoginAvailable()).resolves.toBe(true)
  })

  it('探测接口失败时按不可用处理', async () => {
    installCredentialMock()
    fetchMock.mockRejectedValue(new Error('网络断开'))
    await expect(passkeyLoginAvailable()).resolves.toBe(false)
  })
})

describe('webauthn 登录断言', () => {
  it('把浏览器凭据序列化为 L3 JSON 并回传验证结果', async () => {
    const getMock = vi.fn().mockResolvedValue({
      id: 'cred-1',
      rawId: utf8Buffer('cred-1'),
      type: 'public-key',
      response: {
        authenticatorData: utf8Buffer('authdata'),
        clientDataJSON: utf8Buffer('{"type":"webauthn.get"}'),
        signature: utf8Buffer('sig'),
        userHandle: utf8Buffer('user'),
      },
    })
    installCredentialMock(undefined, getMock)
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ challenge: 'abc', rpId: 'example.com' }))
      .mockResolvedValueOnce(jsonResponse({ valid: true, message: '登录成功' }))

    const result = await loginWithPasskey()

    expect(result.valid).toBe(true)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    const [optionsUrl, optionsInit] = fetchMock.mock.calls[0]
    expect(optionsUrl).toContain('/api/webui/auth/passkey/options')
    expect(optionsInit.method).toBe('GET')
    // 服务端下发的 base64url challenge 显式转成 buffer 再交给认证器
    expect(getMock.mock.calls[0][0].publicKey.challenge).toBeInstanceOf(Uint8Array)
    const [loginUrl, loginInit] = fetchMock.mock.calls[1]
    expect(loginUrl).toContain('/api/webui/auth/passkey/login')
    const body = JSON.parse(loginInit.body)
    expect(body.id).toBe('cred-1')
    expect(body.type).toBe('public-key')
    expect(body.response.authenticatorData).toBe(bytesToBase64url(new TextEncoder().encode('authdata')))
    expect(body.response.userHandle).toBe(bytesToBase64url(new TextEncoder().encode('user')))
    expect(body.response.signature).toBe(bytesToBase64url(new TextEncoder().encode('sig')))
  })

  it('userHandle 为 null 时请求体对应字段为 null', async () => {
    installCredentialMock(undefined, vi.fn().mockResolvedValue({
      id: 'cred-1',
      rawId: utf8Buffer('cred-1'),
      type: 'public-key',
      response: {
        authenticatorData: utf8Buffer('a'),
        clientDataJSON: utf8Buffer('{}'),
        signature: utf8Buffer('s'),
        userHandle: null,
      },
    }))
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ challenge: 'abc' }))
      .mockResolvedValueOnce(jsonResponse({ valid: true }))
    const result = await loginWithPasskey()
    const body = JSON.parse(fetchMock.mock.calls[1][1].body)
    expect(body.response.userHandle).toBeNull()
    expect(result.valid).toBe(true)
  })

  it('认证器抛出 NotAllowedError 时转换为 passkey.* 文案键', async () => {
    installCredentialMock(undefined, vi.fn().mockRejectedValue(new DOMException('用户取消', 'NotAllowedError')))
    fetchMock.mockResolvedValue(jsonResponse({ challenge: 'abc' }))
    await expect(loginWithPasskey()).rejects.toThrow('passkey.cancelled')
  })

  it('服务端判验证失败时透传 valid:false 与消息', async () => {
    installCredentialMock(undefined, vi.fn().mockResolvedValue({
      id: 'cred-1',
      rawId: utf8Buffer('cred-1'),
      type: 'public-key',
      response: {
        authenticatorData: utf8Buffer('a'),
        clientDataJSON: utf8Buffer('{}'),
        signature: utf8Buffer('s'),
        userHandle: null,
      },
    }))
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ challenge: 'abc' }))
      .mockResolvedValueOnce(jsonResponse({ valid: false, message: '通行密钥验证失败' }))
    const result = await loginWithPasskey()
    expect(result.valid).toBe(false)
    expect(result.message).toBe('通行密钥验证失败')
  })
})

describe('webauthn 注册与管理', () => {
  it('注册流程携带设备名并回传注册响应', async () => {
    const createMock = vi.fn().mockResolvedValue({
      id: 'new-cred',
      rawId: utf8Buffer('new-cred'),
      type: 'public-key',
      response: {
        clientDataJSON: utf8Buffer('{"type":"webauthn.create"}'),
        attestationObject: utf8Buffer('attestation'),
        getTransports: () => ['internal'],
      },
    })
    installCredentialMock(createMock)
    fetchMock
      .mockResolvedValueOnce(jsonResponse({
        challenge: 'reg',
        rp: {},
        user: { id: 'user-handle' },
        pubKeyCredParams: [],
        excludeCredentials: [{ id: 'existing', type: 'public-key' }],
      }))
      // 宿主契约是 success 字段，前端 registerPasskey 负责映射成 ok。
      .mockResolvedValueOnce(jsonResponse({ success: true, message: '通行密钥已添加' }))

    const result = await registerPasskey('macOS')

    expect(result.ok).toBe(true)
    const [optionsUrl, optionsInit] = fetchMock.mock.calls[0]
    expect(optionsUrl).toContain('/api/webui/auth/passkey/register/options')
    expect(optionsInit.method).toBe('POST')
    // creation options 的 challenge / user.id / excludeCredentials[].id 都转成了 buffer
    const prepared = createMock.mock.calls[0][0].publicKey
    expect(prepared.challenge).toBeInstanceOf(Uint8Array)
    expect(prepared.user.id).toBeInstanceOf(Uint8Array)
    expect(prepared.excludeCredentials[0].id).toBeInstanceOf(Uint8Array)
    const body = JSON.parse(fetchMock.mock.calls[1][1].body)
    expect(body.name).toBe('macOS')
    expect(body.response.attestationObject).toBe(bytesToBase64url(new TextEncoder().encode('attestation')))
    expect(body.response.transports).toEqual(['internal'])
  })

  it('列表、删除与重命名命中对应端点', async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ passkeys: [{
        credential_id: 'id-1', name: 'macOS', created_at: 1, last_used_at: null,
        transports: ['internal'], backed_up: true,
      }] }))
      .mockResolvedValueOnce(jsonResponse({ success: true }))
      .mockResolvedValueOnce(jsonResponse({ success: true }))

    const list = await listPasskeys()
    expect(list).toHaveLength(1)
    expect(list[0].name).toBe('macOS')

    await deletePasskey('id-1')
    expect(fetchMock.mock.calls[1][0]).toContain('/api/webui/auth/passkeys/id-1')
    expect(fetchMock.mock.calls[1][1].method).toBe('DELETE')

    await renamePasskey('id-1', '笔记本')
    expect(fetchMock.mock.calls[2][0]).toContain('/api/webui/auth/passkeys/id-1')
    expect(fetchMock.mock.calls[2][1].method).toBe('PATCH')
    expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({ name: '笔记本' })
  })
})
