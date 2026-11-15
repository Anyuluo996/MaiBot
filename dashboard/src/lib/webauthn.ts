/**
 * WebAuthn 通行密钥：登录断言与注册管理的浏览器端封装。
 *
 * 宿主下发 camelCase 的 PublicKeyCredential*Options JSON，这里原样交给
 * navigator.credentials，再把浏览器凭据按 WebAuthn L3 JSON 序列化回传。
 * 全部走 authApi（401 透传不跳转），让登录页/安全页自行决定如何呈现错误。
 */
import { authApi } from '@/lib/http'
import { isElectron } from '@/lib/runtime'

/** 安全页通行密钥列表条目（服务端公开视图，不含公钥） */
export interface PasskeySummary {
  credential_id: string
  name: string
  created_at: number | null
  last_used_at: number | null
  transports: string[] | null
  backed_up: boolean
}

export interface PasskeyCeremonyResult {
  ok: boolean
  message?: string
  credentialId?: string
}

/** 浏览器是否具备 WebAuthn 能力（浏览器与 Electron WebView2 均暴露 API；可用性另见 passkeyLoginAvailable）。 */
export function webAuthnAvailable(): boolean {
  return (
    typeof window !== 'undefined' &&
    'PublicKeyCredential' in window &&
    typeof window.PublicKeyCredential === 'function' &&
    typeof window.navigator?.credentials?.create === 'function' &&
    typeof window.navigator?.credentials?.get === 'function'
  )
}

/**
 * 登录页是否应展示通行密钥按钮：浏览器支持 WebAuthn 且宿主已注册过通行密钥。
 * Electron 生产端运行在 app:// 特权协议下没有可注册的 RP 域，仪式必然失败，直接隐藏入口。
 * 探测失败按不可用处理，登录页回退为纯密码输入。
 */
export async function passkeyLoginAvailable(): Promise<boolean> {
  if (!webAuthnAvailable()) return false
  if (isElectron()) return false
  try {
    const data = await authApi.get<{ passkey_available?: boolean }>('/api/webui/auth/check', {
      cache: 'no-store',
    })
    return data?.passkey_available === true
  } catch {
    return false
  }
}

function bufferToBase64url(value: ArrayBuffer | null): string | null {
  if (!value) return null
  const bytes = new Uint8Array(value)
  let binary = ''
  for (let index = 0; index < bytes.length; index += 1) {
    binary += String.fromCharCode(bytes[index])
  }
  return window.btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/** base64url 字符串 → Uint8Array；已是二进制则原样返回。 */
function base64urlToBytes(value: unknown): unknown {
  if (typeof value !== 'string') return value
  const normalized = value.replace(/-/g, '+').replace(/_/g, '/')
  const padded = normalized + '='.repeat((4 - (normalized.length % 4)) % 4)
  const binary = window.atob(padded)
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index)
  }
  return bytes
}

/** 凭据描述符列表（allowCredentials / excludeCredentials）的 id 转 buffer。 */
function prepareCredentialDescriptors(items: unknown): unknown {
  if (!Array.isArray(items)) return items
  return items.map((item) =>
    typeof item === 'object' && item !== null
      ? { ...(item as Record<string, unknown>), id: base64urlToBytes((item as Record<string, unknown>).id) }
      : item,
  )
}

/**
 * 服务端下发的 options 是 base64url 字符串形态；显式转成 buffer 再交给
 * navigator.credentials，不依赖浏览器对 JSON 序列化 options 的支持。
 */
function prepareAssertionOptions(options: Record<string, unknown>): Record<string, unknown> {
  return {
    ...options,
    challenge: base64urlToBytes(options.challenge),
    allowCredentials: prepareCredentialDescriptors(options.allowCredentials),
  }
}

function prepareCreationOptions(options: Record<string, unknown>): Record<string, unknown> {
  const prepared: Record<string, unknown> = {
    ...options,
    challenge: base64urlToBytes(options.challenge),
    excludeCredentials: prepareCredentialDescriptors(options.excludeCredentials),
  }
  if (typeof options.user === 'object' && options.user !== null) {
    const user = options.user as Record<string, unknown>
    prepared.user = { ...user, id: base64urlToBytes(user.id) }
  }
  return prepared
}

/** 把浏览器凭据对象序列化为服务端能直接校验的 JSON（L3 dict 形态）。 */
function serializeCredential(credential: PublicKeyCredential): Record<string, unknown> {
  const response = credential.response
  if ('attestationObject' in response) {
    const attestation = response as AuthenticatorAttestationResponse
    return {
      id: credential.id,
      rawId: bufferToBase64url(credential.rawId),
      type: credential.type,
      response: {
        attestationObject: bufferToBase64url(attestation.attestationObject),
        clientDataJSON: bufferToBase64url(attestation.clientDataJSON),
        transports: attestation.getTransports?.() ?? [],
      },
    }
  }
  const assertion = response as AuthenticatorAssertionResponse
  return {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    response: {
      authenticatorData: bufferToBase64url(assertion.authenticatorData),
      clientDataJSON: bufferToBase64url(assertion.clientDataJSON),
      signature: bufferToBase64url(assertion.signature),
      userHandle: bufferToBase64url(assertion.userHandle),
    },
  }
}

function ceremonyErrorMessage(error: unknown): string {
  if (error instanceof DOMException) {
    if (error.name === 'NotAllowedError') {
      // 用户取消、超时或认证器拒绝：浏览器原始消息不稳定，统一走文案键。
      return 'passkey.cancelled'
    }
    if (error.name === 'InvalidStateError') {
      return 'passkey.invalidState'
    }
    if (error.name === 'SecurityError') {
      return 'passkey.securityError'
    }
  }
  if (error instanceof TypeError) {
    // 老浏览器不认 JSON 形态的 options 等场景：不要把 TypeError 原文回显给用户。
    return 'passkey.failed'
  }
  return error instanceof Error && error.message ? error.message : 'passkey.failed'
}

export interface PasskeyLoginResult {
  valid: boolean
  message?: string
  is_first_setup?: boolean
  requires_custom_token?: boolean
}

/** 通行密钥登录：拉取断言选项 → 本地认证器签名 → 回传验证。 */
export async function loginWithPasskey(): Promise<PasskeyLoginResult> {
  const options = await authApi.get<Record<string, unknown>>('/api/webui/auth/passkey/options', {
    cache: 'no-store',
    errorMessage: 'passkey.optionsFailed',
  })
  let credential: PublicKeyCredential
  try {
    credential = (await window.navigator.credentials.get({
      publicKey: prepareAssertionOptions(options) as unknown as PublicKeyCredentialRequestOptions,
    })) as PublicKeyCredential
  } catch (error) {
    throw new Error(ceremonyErrorMessage(error))
  }
  if (!credential) {
    throw new Error('passkey.cancelled')
  }
  return authApi.post<PasskeyLoginResult>('/api/webui/auth/passkey/login', {
    body: serializeCredential(credential),
    errorMessage: 'passkey.verifyFailed',
  })
}

/** 注册新通行密钥：name 由调用方先行收集，随注册响应一起入库。 */
export async function registerPasskey(name: string): Promise<PasskeyCeremonyResult> {
  const options = await authApi.post<Record<string, unknown>>(
    '/api/webui/auth/passkey/register/options',
    { errorMessage: 'passkey.optionsFailed' },
  )
  let credential: PublicKeyCredential
  try {
    credential = (await window.navigator.credentials.create({
      publicKey: prepareCreationOptions(options) as unknown as PublicKeyCredentialCreationOptions,
    })) as PublicKeyCredential
  } catch (error) {
    throw new Error(ceremonyErrorMessage(error))
  }
  if (!credential) {
    throw new Error('passkey.cancelled')
  }
  // 宿主返回 {"success": bool, "message", "credential_id"}，这里映射成组件侧的 CeremonyResult 形态。
  const data = await authApi.post<{ success: boolean; message?: string; credential_id?: string }>(
    '/api/webui/auth/passkey/register',
    {
      body: { ...serializeCredential(credential), name },
      errorMessage: 'passkey.registerFailed',
    },
  )
  return { ok: data.success === true, message: data.message, credentialId: data.credential_id }
}

export async function listPasskeys(): Promise<PasskeySummary[]> {
  const data = await authApi.get<{ passkeys: PasskeySummary[] }>('/api/webui/auth/passkeys', {
    errorMessage: 'passkey.listFailed',
  })
  return Array.isArray(data?.passkeys) ? data.passkeys : []
}

export async function deletePasskey(credentialId: string): Promise<void> {
  await authApi.delete(`/api/webui/auth/passkeys/${encodeURIComponent(credentialId)}`, {
    errorMessage: 'passkey.deleteFailed',
  })
}

export async function renamePasskey(credentialId: string, name: string): Promise<void> {
  await authApi.patch(`/api/webui/auth/passkeys/${encodeURIComponent(credentialId)}`, {
    body: { name },
    errorMessage: 'passkey.renameFailed',
  })
}
