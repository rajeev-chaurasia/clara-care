'use client'

import { useEffect, useState, useRef } from 'react'
import { Phone, PhoneOff } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface CallStatus {
  is_active: boolean
  call_sid?: string
  patient_id?: string
  duration_sec?: number
  started_at?: string
}

interface LiveCallStatusProps {
  patientId: string
  compact?: boolean
}

function formatDuration(seconds: number): string {
  const mins = Math.floor(seconds / 60)
  const secs = seconds % 60
  return `${mins}:${secs.toString().padStart(2, '0')}`
}

export default function LiveCallStatus({ patientId, compact = false }: LiveCallStatusProps) {
  const [callStatus, setCallStatus] = useState<CallStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [lastChecked, setLastChecked] = useState<Date>(new Date())
  const [connected, setConnected] = useState(false)
  const durationTimerRef = useRef<NodeJS.Timeout | null>(null)
  const startedAtRef = useRef<string | null>(null)

  useEffect(() => {
    let eventSource: EventSource | null = null
    let reconnectTimeout: NodeJS.Timeout

    function connect() {
      eventSource = new EventSource(`/api/call-events?patient_id=${patientId}`)

      eventSource.onopen = () => {
        setConnected(true)
      }

      eventSource.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)
          setLastChecked(new Date())
          setLoading(false)

          if (data.type === 'status' || data.type === 'call_started') {
            setCallStatus({
              is_active: data.is_active,
              call_sid: data.call_sid,
              patient_id: data.patient_id,
              duration_sec: data.duration_sec || 0,
              started_at: data.started_at,
            })
            if (data.is_active && data.started_at) {
              startedAtRef.current = data.started_at
            }
          } else if (data.type === 'call_ended') {
            setCallStatus({
              is_active: false,
              patient_id: data.patient_id,
            })
            startedAtRef.current = null
          }
        } catch (err) {
          console.error('Error parsing SSE event:', err)
        }
      }

      eventSource.onerror = () => {
        setConnected(false)
        eventSource?.close()
        // Reconnect after 5 seconds
        reconnectTimeout = setTimeout(connect, 5000)
      }
    }

    connect()

    return () => {
      eventSource?.close()
      if (reconnectTimeout) clearTimeout(reconnectTimeout)
    }
  }, [patientId])

  // Client-side duration timer: updates every second during active calls
  // so we don't need to poll the server for duration updates
  useEffect(() => {
    if (callStatus?.is_active && startedAtRef.current) {
      // Update duration locally every second
      durationTimerRef.current = setInterval(() => {
        if (startedAtRef.current) {
          const elapsed = Math.floor(
            (Date.now() - new Date(startedAtRef.current).getTime()) / 1000
          )
          setCallStatus((prev) =>
            prev ? { ...prev, duration_sec: elapsed } : prev
          )
        }
      }, 1000)
    } else {
      if (durationTimerRef.current) {
        clearInterval(durationTimerRef.current)
        durationTimerRef.current = null
      }
    }

    return () => {
      if (durationTimerRef.current) {
        clearInterval(durationTimerRef.current)
      }
    }
  }, [callStatus?.is_active])

  if (loading) {
    return (
      <div className={cn(
        "flex items-center gap-2 rounded-xl bg-white px-4 py-3 shadow-sm ring-1 ring-gray-900/5",
        compact ? "flex-row" : "flex-col items-start"
      )}>
        <div className="h-2 w-2 animate-pulse rounded-full bg-gray-300" />
        <span className="text-xs text-gray-500">Connecting...</span>
      </div>
    )
  }

  const isActive = callStatus?.is_active

  if (!isActive) {
    return (
      <div className={cn(
        "flex items-center gap-2 rounded-xl bg-gray-50 px-4 py-3 shadow-sm ring-1 ring-gray-200/60",
        compact ? "flex-row" : "flex-col items-start"
      )}>
        <div className="flex h-8 w-8 items-center justify-center rounded-full bg-gray-200">
          <PhoneOff className="h-4 w-4 text-gray-400" />
        </div>
        <div className={cn("flex-1", !compact && "flex items-center justify-between")}>
          <div>
            <p className="text-xs font-medium text-gray-600">No active call</p>
            <p className="text-[10px] text-gray-400">Clara will call at the scheduled time</p>
          </div>
          {!compact && (
            <div className="flex items-center gap-1.5">
              <span className={cn(
                "h-1.5 w-1.5 rounded-full",
                connected ? "bg-green-400" : "bg-red-400"
              )} />
              <p className="text-[9px] text-gray-400">
                {connected ? 'Live' : 'Reconnecting...'}
              </p>
            </div>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className={cn(
      "relative overflow-hidden rounded-xl bg-gradient-to-br from-emerald-50 to-green-50 px-4 py-3 shadow-sm ring-1 ring-emerald-200",
      compact ? "flex items-center gap-2" : "flex flex-col items-start gap-2"
    )}>
      {/* Animated background pulse */}
      <div className="absolute inset-0 overflow-hidden">
        <div className="absolute -left-1/2 -top-1/2 h-[200%] w-[200%] animate-pulse rounded-full bg-emerald-100/30 blur-3xl" />
      </div>
      
      <div className="relative z-10 flex items-center gap-3">
        {/* Pulsing green dot with rings */}
        <div className="relative flex h-10 w-10 shrink-0 items-center justify-center">
          {/* Outer pulse rings */}
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
          <span className="absolute inline-flex h-3/4 w-3/4 animate-pulse rounded-full bg-emerald-300 opacity-50" />
          {/* Inner solid dot */}
          <span className="relative inline-flex h-4 w-4 shrink-0 rounded-full bg-gradient-to-br from-emerald-400 to-green-500 shadow-lg ring-2 ring-white" />
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <Phone className="h-3.5 w-3.5 text-emerald-600" />
            <p className="text-xs font-semibold text-emerald-900">Clara is on a call</p>
          </div>
          <div className="mt-0.5 flex items-center gap-2">
            <span className="inline-flex items-center rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-medium text-emerald-700">
              <span className="mr-1 h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500" />
              Live
            </span>
            {callStatus.duration_sec !== undefined && (
              <span className="text-[10px] text-emerald-600">
                {formatDuration(callStatus.duration_sec)}
              </span>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
