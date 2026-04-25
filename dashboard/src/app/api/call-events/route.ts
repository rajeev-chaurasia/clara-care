import { NextRequest } from 'next/server'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

/**
 * SSE proxy: pipes the backend's event stream through to the browser.
 * This avoids CORS issues and keeps the backend URL private.
 */
export async function GET(request: NextRequest) {
  const patientId = request.nextUrl.searchParams.get('patient_id')

  if (!patientId) {
    return new Response(JSON.stringify({ error: 'patient_id is required' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json' },
    })
  }

  const upstream = await fetch(
    `${API_URL}/api/call-events/stream?patient_id=${encodeURIComponent(patientId)}`,
    {
      headers: { Accept: 'text/event-stream' },
      cache: 'no-store',
    }
  )

  if (!upstream.ok || !upstream.body) {
    return new Response(
      JSON.stringify({ error: 'Failed to connect to call event stream' }),
      { status: upstream.status, headers: { 'Content-Type': 'application/json' } }
    )
  }

  // Pipe the upstream SSE stream directly through
  return new Response(upstream.body, {
    headers: {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no',
    },
  })
}
