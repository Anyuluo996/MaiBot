import { GitBranch, Loader2 } from 'lucide-react'

import type { ReasoningToolCallRecord } from '@/lib/reasoning-process-api'
import { cn } from '@/lib/utils'
import { formatStageName } from './context-items'

const STEP_EMOJI: Record<string, string> = {
  planner: '🧭',
  replyer: '💬',
  replyer_creative: '💬',
}

function stepEmoji(stage: string): string {
  if (STEP_EMOJI[stage]) return STEP_EMOJI[stage]
  if (stage.includes('expression')) return '🎭'
  if (stage === 'llm_error') return '⚠️'
  if (/memory|retriev|recall|query_|spo_/.test(stage)) return '📚'
  if (/tool|search|fetch/.test(stage)) return '🔍'
  if (/learner|learning|jargon/.test(stage)) return '🧩'
  return '⚙️'
}

/**
 * 同轮关联步骤的横向轨迹条：一轮推理的各模块按时间排列成节点链，
 * 当前记录高亮，点击节点切换到对应记录。
 */
export function RelatedStepsCard({
  records,
  current,
  loading,
  error,
  onSelect,
}: {
  records: ReasoningToolCallRecord[]
  current: { stage: string; session_id: string; stem: string }
  loading: boolean
  error: string | null
  onSelect: (record: ReasoningToolCallRecord) => void
}) {
  return (
    <section
      aria-label="本轮轨迹"
      className="bg-muted/20 mx-2 mt-2 rounded-md border p-2 sm:mx-3 sm:mt-3"
    >
      <div className="text-muted-foreground mb-1.5 flex items-center gap-1.5 text-xs">
        <GitBranch className="h-3.5 w-3.5" />
        <span className="text-foreground font-medium">本轮轨迹</span>
        {loading ? (
          <Loader2 className="h-3 w-3 animate-spin" />
        ) : (
          <span>{records.length} 个步骤</span>
        )}
        <span className="ml-auto hidden sm:inline">按记录时间排列 · 点击节点切换</span>
      </div>
      {error ? (
        <p className="text-destructive text-xs">{error}</p>
      ) : (
        <div className="flex items-start overflow-x-auto pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {records.map((record, index) => {
            const active =
              record.stage === current.stage &&
              record.session === current.session_id &&
              record.stem === current.stem
            const failed = record.stage === 'llm_error'
            const time = new Date(Number(record.stem)).toLocaleTimeString('zh-CN', {
              hour12: false,
            })
            return (
              <div key={`${record.stage}/${record.session}/${record.stem}`} className="flex items-start">
                <button
                  type="button"
                  onClick={() => onSelect(record)}
                  aria-current={active ? 'step' : undefined}
                  className={cn(
                    'flex w-16 shrink-0 cursor-pointer flex-col items-center gap-0.5 rounded-md px-1 py-1.5 transition-colors',
                    active && 'bg-primary/10',
                    !active && 'hover:bg-muted/60'
                  )}
                  title={active ? '当前步骤' : `查看${formatStageName(record.stage)} · ${time}`}
                >
                  <span
                    className={cn(
                      'flex h-7 w-7 items-center justify-center rounded-lg border text-[13px]',
                      active
                        ? 'border-primary bg-primary/15'
                        : failed
                          ? 'border-destructive/40 bg-destructive/10'
                          : 'border-border bg-background'
                    )}
                  >
                    {stepEmoji(record.stage)}
                  </span>
                  <span
                    className={cn(
                      'w-full truncate text-center text-[10.5px] leading-tight',
                      active ? 'text-primary font-semibold' : 'text-muted-foreground'
                    )}
                  >
                    {formatStageName(record.stage)}
                    {active && ' ·'}
                  </span>
                  <span className="text-muted-foreground text-[9.5px] leading-none tabular-nums">
                    {time}
                  </span>
                </button>
                {index < records.length - 1 && (
                  <span aria-hidden="true" className="bg-border mt-[17px] h-0.5 w-4 shrink-0 rounded-full" />
                )}
              </div>
            )
          })}
          {!loading && records.length <= 1 && (
            <span className="text-muted-foreground self-center pl-2 text-xs">尚无其他关联记录</span>
          )}
        </div>
      )}
    </section>
  )
}
