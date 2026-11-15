import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Fingerprint, Loader2, Pencil, Plus, RotateCw, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { useToast } from '@/hooks/use-toast'
import { isElectron } from '@/lib/runtime'
import {
  deletePasskey,
  listPasskeys,
  registerPasskey,
  renamePasskey,
  webAuthnAvailable,
  type PasskeySummary,
} from '@/lib/webauthn'

/** 从 UA 猜一个语言无关的默认设备名，供注册弹窗预填。 */
function guessDeviceName(): string {
  if (typeof navigator === 'undefined') return 'Web'
  const agent = navigator.userAgent
  if (/iPhone|iPad/i.test(agent)) return 'iOS'
  if (/Android/i.test(agent)) return 'Android'
  if (/Windows/i.test(agent)) return 'Windows'
  if (/Mac/i.test(agent)) return 'macOS'
  if (/Linux/i.test(agent)) return 'Linux'
  return 'Web'
}

function formatTimestamp(seconds: number | null, neverLabel: string): string {
  if (!seconds) return neverLabel
  const date = new Date(seconds * 1000)
  return Number.isNaN(date.getTime()) ? neverLabel : date.toLocaleString()
}

export function PasskeyCard() {
  const { t } = useTranslation()
  const { toast } = useToast()
  const [passkeys, setPasskeys] = useState<PasskeySummary[]>([])
  const [listReady, setListReady] = useState(false)
  const [listError, setListError] = useState(false)
  const [addOpen, setAddOpen] = useState(false)
  const [deviceName, setDeviceName] = useState('')
  const [registering, setRegistering] = useState(false)
  const [renameTarget, setRenameTarget] = useState<PasskeySummary | null>(null)
  const [renameName, setRenameName] = useState('')
  const [deleting, setDeleting] = useState<PasskeySummary | null>(null)
  const [deletingBusy, setDeletingBusy] = useState(false)

  /** passkey.* 文案键转译文，其余原样展示（服务端消息）。 */
  const ceremonyText = useCallback(
    (error: unknown): string => {
      const message = error instanceof Error ? error.message : ''
      return message.startsWith('passkey.') ? t(message) : message
    },
    [t],
  )

  const refresh = useCallback(async () => {
    // 加载失败要显式暴露错误态而不是伪装成空列表，否则用户会误以为密钥全部消失。
    try {
      setPasskeys(await listPasskeys())
      setListError(false)
    } catch (error) {
      setPasskeys([])
      setListError(true)
      console.error('通行密钥列表加载失败:', error)
    } finally {
      setListReady(true)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const handleStartRegister = () => {
    setDeviceName(guessDeviceName())
    setAddOpen(true)
  }

  const handleRegister = async () => {
    setRegistering(true)
    try {
      const result = await registerPasskey(deviceName.trim() || guessDeviceName())
      if (result.ok) {
        toast({
          title: t('settings.security.passkeyAddSuccess'),
          description: t('settings.security.passkeyAddSuccessDesc'),
        })
        setAddOpen(false)
        await refresh()
      } else {
        toast({
          title: t('settings.security.passkeyAddFailed'),
          description: result.message || t('settings.security.passkeyTryAgain'),
          variant: 'destructive',
        })
      }
    } catch (error) {
      toast({
        title: t('settings.security.passkeyAddFailed'),
        description: ceremonyText(error),
        variant: 'destructive',
      })
    } finally {
      setRegistering(false)
    }
  }

  const handleRename = async () => {
    if (!renameTarget) return
    try {
      await renamePasskey(renameTarget.credential_id, renameName.trim() || renameTarget.name)
      toast({ title: t('settings.security.passkeyRenameSuccess') })
      setRenameTarget(null)
      await refresh()
    } catch (error) {
      toast({
        title: t('settings.security.passkeyRenameFailed'),
        description: ceremonyText(error),
        variant: 'destructive',
      })
    }
  }

  const handleDelete = async () => {
    if (!deleting) return
    setDeletingBusy(true)
    try {
      await deletePasskey(deleting.credential_id)
      toast({ title: t('settings.security.passkeyDeleteSuccess') })
      setDeleting(null)
      await refresh()
    } catch (error) {
      toast({
        title: t('settings.security.passkeyDeleteFailed'),
        description: ceremonyText(error),
        variant: 'destructive',
      })
    } finally {
      setDeletingBusy(false)
    }
  }

  return (
    <Card className="p-4 sm:p-6">
      <div className="mb-3 flex flex-col gap-3 sm:mb-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h3 className="text-base font-semibold sm:text-lg">{t('settings.security.passkeyTitle')}</h3>
          <p className="text-muted-foreground mt-1 text-sm">{t('settings.security.passkeyDesc')}</p>
        </div>
        <Button
          onClick={handleStartRegister}
          // Electron 的 app:// 协议没有可注册 RP 域，与登录页一致地隐藏入口。
          disabled={registering || !webAuthnAvailable() || isElectron()}
          className="gap-2 w-full sm:w-auto"
          data-testid="passkey-add-button"
        >
          {registering ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Plus className="h-4 w-4" />
          )}
          {t('settings.security.passkeyAdd')}
        </Button>
      </div>

      {passkeys.length === 0 ? (
        <div
          data-testid="passkey-empty"
          className={listError
            ? 'flex items-center justify-between gap-3 rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm'
            : 'text-muted-foreground flex items-center gap-2 rounded-lg border border-dashed p-4 text-sm'}
        >
          <div className="flex min-w-0 items-center gap-2">
            <Fingerprint className="h-4 w-4 flex-shrink-0" />
            {listError ? t('passkey.listFailed') : listReady ? t('settings.security.passkeyEmpty') : t('settings.security.passkeyLoading')}
          </div>
          {listError && (
            <Button variant="outline" size="sm" className="flex-shrink-0 gap-2" onClick={() => void refresh()}>
              <RotateCw className="h-3.5 w-3.5" />
              {t('settings.security.passkeyRetry')}
            </Button>
          )}
        </div>
      ) : (
        <ul className="divide-y" data-testid="passkey-list">
          {passkeys.map((passkey) => (
            <li key={passkey.credential_id} className="flex items-center gap-3 py-3" data-testid="passkey-item">
              <Fingerprint className="text-muted-foreground h-5 w-5 flex-shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate text-sm font-medium">{passkey.name}</span>
                  {passkey.backed_up && (
                    <Badge variant="secondary" className="text-[10px]">
                      {t('settings.security.passkeyBackedUp')}
                    </Badge>
                  )}
                </div>
                <p className="text-muted-foreground mt-0.5 text-xs">
                  {t('settings.security.passkeyCreated')}{' '}
                  {formatTimestamp(passkey.created_at, '-')}
                  {' · '}
                  {t('settings.security.passkeyLastUsed')}{' '}
                  {formatTimestamp(passkey.last_used_at, t('settings.security.passkeyNeverUsed'))}
                </p>
              </div>
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8 flex-shrink-0"
                aria-label={`${t('settings.security.passkeyRename')}: ${passkey.name}`}
                title={t('settings.security.passkeyRename')}
                onClick={() => {
                  setRenameTarget(passkey)
                  setRenameName(passkey.name)
                }}
              >
                <Pencil className="h-3.5 w-3.5" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className="text-destructive hover:text-destructive h-8 w-8 flex-shrink-0"
                aria-label={`${t('settings.security.passkeyDelete')}: ${passkey.name}`}
                title={t('settings.security.passkeyDelete')}
                onClick={() => setDeleting(passkey)}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </li>
          ))}
        </ul>
      )}

      {/* 注册弹窗：先收集设备名，再由浏览器弹出认证器。 */}
      <Dialog open={addOpen} onOpenChange={(open) => { if (!registering) setAddOpen(open) }}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t('settings.security.passkeyAddTitle')}</DialogTitle>
            <DialogDescription>{t('settings.security.passkeyAddDesc')}</DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="passkey-name" className="text-sm">
              {t('settings.security.passkeyNameLabel')}
            </Label>
            <Input
              id="passkey-name"
              value={deviceName}
              onChange={(event) => setDeviceName(event.target.value)}
              placeholder={t('settings.security.passkeyNamePlaceholder')}
              maxLength={100}
              disabled={registering}
            />
          </div>
          <DialogFooter className="gap-2 sm:gap-0">
            <Button variant="outline" disabled={registering} onClick={() => setAddOpen(false)}>
              {t('settings.security.cancel')}
            </Button>
            <Button onClick={() => void handleRegister()} disabled={registering} className="gap-2">
              {registering && <Loader2 className="h-4 w-4 animate-spin" />}
              {registering
                ? t('settings.security.passkeyRegistering')
                : t('settings.security.passkeyStart')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 重命名弹窗 */}
      <Dialog
        open={renameTarget !== null}
        onOpenChange={(open) => { if (!open) setRenameTarget(null) }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t('settings.security.passkeyRenameTitle')}</DialogTitle>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="passkey-rename" className="text-sm">
              {t('settings.security.passkeyNameLabel')}
            </Label>
            <Input
              id="passkey-rename"
              value={renameName}
              onChange={(event) => setRenameName(event.target.value)}
              maxLength={100}
            />
          </div>
          <DialogFooter className="gap-2 sm:gap-0">
            <Button variant="outline" onClick={() => setRenameTarget(null)}>
              {t('settings.security.cancel')}
            </Button>
            <Button onClick={() => void handleRename()}>
              {t('settings.security.passkeyRenameSave')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 删除确认 */}
      <Dialog
        open={deleting !== null}
        onOpenChange={(open) => { if (!open && !deletingBusy) setDeleting(null) }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t('settings.security.passkeyDeleteConfirmTitle')}</DialogTitle>
            <DialogDescription>
              {t('settings.security.passkeyDeleteConfirmDesc', { name: deleting?.name ?? '' })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2 sm:gap-0">
            <Button variant="outline" disabled={deletingBusy} onClick={() => setDeleting(null)}>
              {t('settings.security.cancel')}
            </Button>
            <Button variant="destructive" disabled={deletingBusy} onClick={() => void handleDelete()}>
              {deletingBusy
                ? t('settings.security.passkeyDeleting')
                : t('settings.security.passkeyDelete')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}
