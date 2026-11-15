import { Link } from '@tanstack/react-router'
import { CircleUserRound, LogOut, ShieldCheck } from 'lucide-react'
import type { ComponentType } from 'react'
import { useTranslation } from 'react-i18next'

import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { logout } from '@/lib/auth'
import { cn } from '@/lib/utils'

interface UserMenuProps {
  /** 菜单弹出方向：侧栏底部工具条向上，顶栏向下 */
  side?: 'top' | 'bottom'
  align?: 'start' | 'center' | 'end'
  /** 触发器形态：observation-key 适配麦麦观察布局的键位样式 */
  variant?: 'ghost' | 'observation-key'
  className?: string
}

/**
 * 账号菜单：安全设置入口与登出。
 * 登出原先埋在「设置 → 安全」页深处，两套布局壳的工具条都接入此菜单后一键可达。
 */
export function UserMenu({ side = 'bottom', align = 'end', variant = 'ghost', className }: UserMenuProps) {
  const { t } = useTranslation()
  const menuLabel = t('user.menuLabel')
  const TriggerIcon = CircleUserRound as ComponentType<{ className?: string }>

  const trigger =
    variant === 'observation-key' ? (
      <button
        type="button"
        data-observation-icon-key="true"
        data-observation-user-menu="true"
        aria-label={menuLabel}
        title={menuLabel}
      >
        <TriggerIcon className="h-5 w-5" />
      </button>
    ) : (
      <Button
        variant="ghost"
        size="icon"
        className={cn('border-0 bg-transparent shadow-none', className)}
        aria-label={menuLabel}
        title={menuLabel}
        data-dashboard-user-menu="true"
      >
        <TriggerIcon className="h-[1.1rem] w-[1.1rem]" />
      </Button>
    )

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>{trigger}</DropdownMenuTrigger>
      <DropdownMenuContent side={side} align={align} sideOffset={8} className="w-44">
        <DropdownMenuItem asChild>
          <Link to="/settings" search={{ tab: 'security' }} className="cursor-pointer gap-2">
            <ShieldCheck className="h-4 w-4" />
            {t('user.securitySettings')}
          </Link>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          className="text-destructive focus:text-destructive cursor-pointer gap-2"
          onClick={() => void logout()}
        >
          <LogOut className="h-4 w-4" />
          {t('header.logoutLabel')}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
