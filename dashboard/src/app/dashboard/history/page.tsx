'use client'

import { useState, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { MessageSquare } from 'lucide-react'
import TopBar from '@/components/TopBar'
import LoadingSpinner from '@/components/LoadingSpinner'
import ConversationCard from '@/components/ConversationCard'
import EmptyState from '@/components/EmptyState'
import { getConversations, getPatientId } from '@/lib/api'
import type { Conversation } from '@/lib/api'
import { cn } from '@/lib/utils'

const MOOD_FILTERS = ['All', 'Happy', 'Nostalgic', 'Neutral', 'Sad', 'Confused', 'Distressed'] as const

export default function HistoryPage() {
  const router = useRouter()
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [activeFilter, setActiveFilter] = useState<string>('All')

  useEffect(() => {
    async function load() {
      try {
        const data = await getConversations(getPatientId(), 100)
        setConversations(data)
      } catch {
        setError('Failed to load conversations')
      } finally {
        setLoading(false)
      }
    }
    load()
  }, [])

  const filtered =
    activeFilter === 'All'
      ? conversations
      : conversations.filter(
        (c) => c.detected_mood.toLowerCase() === activeFilter.toLowerCase()
      )

  return (
    <>
      <TopBar title="Conversation History" subtitle="Browse what Clara talked about">
        <div className="overflow-x-auto pt-1" style={{ scrollbarWidth: 'none' }}>
          <div className="flex gap-2">
            {MOOD_FILTERS.map((filter) => (
              <button
                key={filter}
                onClick={() => setActiveFilter(filter)}
                className={cn(
                  'shrink-0 rounded-full px-4 py-1.5 text-xs font-medium transition-all duration-200',
                  activeFilter === filter
                    ? 'bg-clara-600 text-white shadow-sm ring-1 ring-clara-600'
                    : 'bg-gray-50 text-gray-600 ring-1 ring-gray-200/60 hover:bg-gray-100 hover:text-gray-900'
                )}
                type="button"
              >
                {filter}
              </button>
            ))}
          </div>
        </div>
      </TopBar>

      <main className="space-y-4 px-4 py-4">
        {conversations.length > 0 && !loading && !error && (
          <section className="rounded-2xl bg-white p-3 shadow-sm" aria-label="History summary">
            <p className="text-xs text-gray-600">
              You have <span className="font-semibold text-gray-900">{conversations.length}</span> recorded
              conversations. Use the filters above to jump to calls by mood.
            </p>
          </section>
        )}
        {loading && <LoadingSpinner />}

        {error && (
          <div className="py-8 text-center text-sm text-red-500">{error}</div>
        )}

        {!loading && !error && filtered.length === 0 && (
          <EmptyState
            icon={MessageSquare}
            title="No conversations found"
            description={
              activeFilter === 'All'
                ? 'No conversations have been recorded yet.'
                : `No conversations with "${activeFilter}" mood found.`
            }
          />
        )}

        {!loading && !error && filtered.length > 0 && (
          <div className="space-y-2">
            {filtered.map((c) => (
              <ConversationCard
                key={c.id}
                conversation={c}
                onClick={() => router.push(`/dashboard/history/${c.id}`)}
              />
            ))}
          </div>
        )}
      </main>
    </>
  )
}
