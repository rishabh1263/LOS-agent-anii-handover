import { Pin, Plus, Trash2 } from 'lucide-react'
import type { Conversation } from '../../../runtime/chatbot'
import { groupConversationsByDate } from '../../../runtime/chatbot'

interface ConversationSidebarProps {
  conversations: Conversation[]
  activeId: string | null
  onSelect: (id: string) => void
  onNew: () => void
  onDelete: (id: string) => void
  open: boolean
}

export function ConversationSidebar({
  conversations,
  activeId,
  onSelect,
  onNew,
  onDelete,
  open,
}: ConversationSidebarProps) {
  if (!open) return null

  const groups = groupConversationsByDate(conversations)

  return (
    <aside className="flex h-full w-[256px] shrink-0 flex-col border-r border-line bg-surface">
      <div className="shrink-0 border-b border-line p-3">
        <button
          type="button"
          onClick={onNew}
          className="flex h-11 w-full items-center justify-center gap-2 rounded-sm border border-line bg-surface font-sans text-[14px] font-medium text-content transition-colors hover:bg-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-ember"
        >
          <Plus className="h-4 w-4" strokeWidth={2} />
          New chat
        </button>
      </div>

      <div className="flex-1 overflow-y-auto py-2">
        {groups.map((g) => (
          <div key={g.label} className="mb-3">
            <div className="px-3 py-1 font-sans text-[11px] font-semibold uppercase tracking-[0.08em] text-content-disabled">
              {g.label}
            </div>
            <ul>
              {g.items.map((c) => {
                const active = c.id === activeId
                return (
                  <li key={c.id} className="relative px-2">
                    <button
                      type="button"
                      onClick={() => onSelect(c.id)}
                      className={`group flex h-11 w-full items-center gap-2 rounded-sm px-3 text-left font-sans text-[14px] transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ember ${
                        active
                          ? 'bg-ember-tint font-medium text-content'
                          : 'text-content-secondary hover:bg-raised hover:text-content'
                      }`}
                    >
                      {active && (
                        <span
                          className="absolute left-0 top-1/2 h-6 w-[3px] -translate-y-1/2 rounded-r-full bg-ember"
                          aria-hidden
                        />
                      )}
                      {c.pinned && (
                        <Pin className="h-3 w-3 shrink-0 text-ember" strokeWidth={2} />
                      )}
                      <span className="min-w-0 flex-1 truncate">{c.title}</span>
                      <span
                        role="button"
                        tabIndex={0}
                        aria-label={`Delete ${c.title}`}
                        onClick={(e) => {
                          e.stopPropagation()
                          onDelete(c.id)
                        }}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            e.stopPropagation()
                            onDelete(c.id)
                          }
                        }}
                        className="flex h-7 w-7 shrink-0 items-center justify-center rounded-xs opacity-0 transition-opacity hover:bg-error-subtle hover:text-danger-text group-hover:opacity-100"
                      >
                        <Trash2 className="h-3.5 w-3.5" strokeWidth={2} />
                      </span>
                    </button>
                  </li>
                )
              })}
            </ul>
          </div>
        ))}
      </div>
    </aside>
  )
}
