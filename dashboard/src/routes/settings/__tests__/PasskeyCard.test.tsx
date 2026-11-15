import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PasskeyCard } from '../PasskeyCard'

const mocks = vi.hoisted(() => ({
  toast: vi.fn(),
  listPasskeys: vi.fn(),
  registerPasskey: vi.fn(),
  deletePasskey: vi.fn(),
  renamePasskey: vi.fn(),
}))

vi.mock('react-i18next', () => {
  const t = (key: string, variables?: Record<string, unknown>) =>
    variables ? `${key}:${JSON.stringify(variables)}` : key
  return { useTranslation: () => ({ t }) }
})

vi.mock('@/hooks/use-toast', () => ({
  useToast: () => ({ toast: mocks.toast }),
}))

vi.mock('@/lib/webauthn', () => ({
  listPasskeys: mocks.listPasskeys,
  registerPasskey: mocks.registerPasskey,
  deletePasskey: mocks.deletePasskey,
  renamePasskey: mocks.renamePasskey,
  webAuthnAvailable: vi.fn(() => true),
}))

/** 轻量替身：open 时渲染 children，行为断言聚焦在卡片状态流转。 */
vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({ children, open }: { children: React.ReactNode; open: boolean }) =>
    open ? <div data-testid="passkey-dialog">{children}</div> : null,
  DialogContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
  DialogDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
  DialogFooter: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}))

const PASSKEY = {
  credential_id: 'cred-1',
  name: 'macOS',
  created_at: 1_700_000_000,
  last_used_at: null,
  transports: ['internal'],
  backed_up: true,
}

beforeEach(() => {
  mocks.listPasskeys.mockResolvedValue([PASSKEY])
  vi.spyOn(console, 'error').mockImplementation(() => undefined)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  mocks.toast.mockReset()
  mocks.registerPasskey.mockReset()
  mocks.deletePasskey.mockReset()
  mocks.renamePasskey.mockReset()
})

describe('PasskeyCard', () => {
  it('加载并展示已注册的通行密钥', async () => {
    render(<PasskeyCard />)
    await waitFor(() => expect(screen.getByTestId('passkey-item')).toBeInTheDocument())
    expect(screen.getByText('macOS')).toBeInTheDocument()
    expect(screen.getByText(/settings.security.passkeyNeverUsed/)).toBeInTheDocument()
    expect(screen.getByText('settings.security.passkeyBackedUp')).toBeInTheDocument()
  })

  it('列表为空时展示空态文案', async () => {
    mocks.listPasskeys.mockResolvedValue([])
    render(<PasskeyCard />)
    await waitFor(() => expect(screen.getByTestId('passkey-empty')).toBeInTheDocument())
    expect(screen.getByText('settings.security.passkeyEmpty')).toBeInTheDocument()
  })

  it('列表加载失败展示错误态而非伪装空态，重试后恢复', async () => {
    mocks.listPasskeys.mockRejectedValueOnce(new Error('网络断开'))
    render(<PasskeyCard />)

    await waitFor(() => expect(screen.getByText('passkey.listFailed')).toBeInTheDocument())
    expect(screen.getByText('settings.security.passkeyRetry')).toBeInTheDocument()

    // 重试成功后回到正常空态
    mocks.listPasskeys.mockResolvedValueOnce([])
    fireEvent.click(screen.getByRole('button', { name: 'settings.security.passkeyRetry' }))
    await waitFor(() => expect(screen.getByText('settings.security.passkeyEmpty')).toBeInTheDocument())
  })

  it('注册流程携带设备名并提示成功后刷新列表', async () => {
    // 服务端真实契约：success 布尔。
    mocks.registerPasskey.mockResolvedValue({ ok: true, credentialId: 'cred-2' })
    render(<PasskeyCard />)

    fireEvent.click(await screen.findByTestId('passkey-add-button'))
    const dialog = await screen.findByTestId('passkey-dialog')
    expect(dialog).toBeInTheDocument()

    const nameInput = screen.getByLabelText('settings.security.passkeyNameLabel')
    fireEvent.change(nameInput, { target: { value: '笔记本指纹' } })
    fireEvent.click(screen.getByRole('button', { name: 'settings.security.passkeyStart' }))

    await waitFor(() => expect(mocks.registerPasskey).toHaveBeenCalledWith('笔记本指纹'))
    await waitFor(() =>
      expect(mocks.toast).toHaveBeenCalledWith(
        expect.objectContaining({ title: 'settings.security.passkeyAddSuccess' }),
      ),
    )
    // 注册成功后重新拉取列表
    await waitFor(() => expect(mocks.listPasskeys).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByTestId('passkey-dialog')).not.toBeInTheDocument())
  })

  it('注册失败时透传 passkey 文案键并保持弹窗', async () => {
    mocks.registerPasskey.mockRejectedValue(new Error('passkey.cancelled'))
    render(<PasskeyCard />)

    fireEvent.click(await screen.findByTestId('passkey-add-button'))
    fireEvent.click(await screen.findByRole('button', { name: 'settings.security.passkeyStart' }))

    await waitFor(() =>
      expect(mocks.toast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: 'settings.security.passkeyAddFailed',
          description: 'passkey.cancelled',
          variant: 'destructive',
        }),
      ),
    )
    expect(screen.getByTestId('passkey-dialog')).toBeInTheDocument()
  })

  it('删除需确认并携带凭据 ID', async () => {
    mocks.deletePasskey.mockResolvedValue(undefined)
    render(<PasskeyCard />)

    fireEvent.click(await screen.findByRole('button', { name: 'settings.security.passkeyDelete: macOS' }))
    fireEvent.click(
      await screen.findByRole('button', { name: 'settings.security.passkeyDelete' }),
    )

    await waitFor(() => expect(mocks.deletePasskey).toHaveBeenCalledWith('cred-1'))
    await waitFor(() =>
      expect(mocks.toast).toHaveBeenCalledWith(
        expect.objectContaining({ title: 'settings.security.passkeyDeleteSuccess' }),
      ),
    )
    await waitFor(() => expect(mocks.listPasskeys).toHaveBeenCalledTimes(2))
  })
})
