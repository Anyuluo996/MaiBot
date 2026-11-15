import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { UserMenu } from '../UserMenu'

const mocks = vi.hoisted(() => ({
  logout: vi.fn(),
}))

vi.mock('react-i18next', () => {
  const t = (key: string) => key
  return { useTranslation: () => ({ t }) }
})

vi.mock('@tanstack/react-router', () => ({
  Link: ({
    children,
    to,
    search,
  }: {
    children: React.ReactNode
    to: string
    search?: Record<string, unknown>
  }) => {
    const query = search
      ? '?' + new URLSearchParams(Object.entries(search).map(([key, value]) => [key, String(value)])).toString()
      : ''
    return (
      <a href={to + query} data-testid="user-menu-link">
        {children}
      </a>
    )
  },
}))

vi.mock('@/lib/auth', () => ({
  logout: mocks.logout,
}))

/** jsdom 未实现 Pointer Capture，Radix 菜单依赖。 */
function stubPointerCapture() {
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false
  }
  if (!Element.prototype.setPointerCapture) {
    Element.prototype.setPointerCapture = () => {}
  }
  if (!Element.prototype.releasePointerCapture) {
    Element.prototype.releasePointerCapture = () => {}
  }
}

beforeEach(() => {
  stubPointerCapture()
})

afterEach(() => {
  cleanup()
  mocks.logout.mockReset()
})

describe('UserMenu', () => {
  it('点击触发器打开菜单，包含安全设置与登出', async () => {
    const user = userEvent.setup()
    render(<UserMenu />)
    await user.click(screen.getByRole('button', { name: 'user.menuLabel' }))

    const link = await screen.findByTestId('user-menu-link')
    expect(link).toHaveAttribute('href', '/settings?tab=security')
    expect(screen.getByText('user.securitySettings')).toBeInTheDocument()
    expect(screen.getByText('header.logoutLabel')).toBeInTheDocument()
  })

  it('点击登出调用 logout', async () => {
    const user = userEvent.setup()
    render(<UserMenu />)
    await user.click(screen.getByRole('button', { name: 'user.menuLabel' }))
    await user.click(await screen.findByText('header.logoutLabel'))

    await waitFor(() => expect(mocks.logout).toHaveBeenCalledTimes(1))
  })

  it('observation-key 变体渲染键位样式触发器', () => {
    render(<UserMenu variant="observation-key" side="top" align="start" />)
    const trigger = screen.getByRole('button', { name: 'user.menuLabel' })
    expect(trigger).toHaveAttribute('data-observation-icon-key', 'true')
  })
})
