"""
Server-Sent Events (SSE) endpoint for real-time call status.

Replaces client-side polling with server-push:
- Dashboard opens ONE persistent connection
- Backend pushes events only when call status actually changes
- Zero network traffic when nothing is happening

Architecture:
    TwilioCallSession.start() / .end()
        → call_event_bus.publish(...)
            → SSE endpoint yields event to every connected dashboard
"""

import asyncio
import json
import logging
from datetime import datetime, UTC
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/call-events", tags=["call-events"])


# ---------------------------------------------------------------------------
# In-process event bus (single-pod; swap for Redis pub/sub if scaling out)
# ---------------------------------------------------------------------------

class CallEventBus:
    """
    Simple broadcast pub/sub using asyncio.Queue per subscriber.
    Thread-safe within one event loop (which is all we need for a single pod).
    """

    def __init__(self):
        self._subscribers: dict[int, asyncio.Queue] = {}
        self._counter = 0

    def subscribe(self) -> tuple[int, asyncio.Queue]:
        """Register a new subscriber. Returns (subscriber_id, queue)."""
        self._counter += 1
        q: asyncio.Queue = asyncio.Queue(maxsize=32)
        self._subscribers[self._counter] = q
        logger.info(f"[SSE] Subscriber {self._counter} connected (total={len(self._subscribers)})")
        return self._counter, q

    def unsubscribe(self, sub_id: int):
        """Remove a subscriber."""
        self._subscribers.pop(sub_id, None)
        logger.info(f"[SSE] Subscriber {sub_id} disconnected (total={len(self._subscribers)})")

    async def publish(self, event: dict):
        """Broadcast an event to all subscribers."""
        data = json.dumps(event)
        dead: list[int] = []
        for sub_id, q in self._subscribers.items():
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                # Slow consumer — drop oldest and push new
                try:
                    q.get_nowait()
                    q.put_nowait(data)
                except Exception:
                    dead.append(sub_id)
        for sub_id in dead:
            self._subscribers.pop(sub_id, None)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


# Global singleton
call_event_bus = CallEventBus()


# ---------------------------------------------------------------------------
# Helper to publish from TwilioCallSession
# ---------------------------------------------------------------------------

async def publish_call_started(
    patient_id: str,
    call_sid: str,
    started_at: Optional[str] = None,
):
    """Publish when a call becomes active."""
    await call_event_bus.publish({
        "type": "call_started",
        "patient_id": patient_id,
        "call_sid": call_sid,
        "started_at": started_at or datetime.now(UTC).isoformat(),
        "is_active": True,
    })


async def publish_call_ended(
    patient_id: str,
    call_sid: str,
    duration_sec: int = 0,
):
    """Publish when a call ends."""
    await call_event_bus.publish({
        "type": "call_ended",
        "patient_id": patient_id,
        "call_sid": call_sid,
        "duration_sec": duration_sec,
        "is_active": False,
    })


# ---------------------------------------------------------------------------
# SSE endpoint
# ---------------------------------------------------------------------------

@router.get("/stream")
async def call_event_stream(patient_id: str = Query(..., description="Patient ID to watch")):
    """
    SSE stream of call status events for a specific patient.

    The dashboard connects once and receives push events:
        - call_started  → show live indicator
        - call_ended    → hide live indicator
        - heartbeat     → keep connection alive (every 30s)

    On connect, immediately sends the current status so the UI is correct
    without waiting for the next event.
    """

    async def event_generator():
        sub_id, queue = call_event_bus.subscribe()
        try:
            # 1. Send current status immediately on connect
            from app.voice import twilio_bridge

            initial_status = {"type": "status", "is_active": False, "patient_id": patient_id}
            for call_sid, session in twilio_bridge.active_calls.items():
                if session.patient_id == patient_id and session.is_active:
                    duration_sec = 0
                    started_at = None
                    if session.call_start_time:
                        duration_sec = int((datetime.now(UTC) - session.call_start_time).total_seconds())
                        started_at = session.call_start_time.isoformat()
                    initial_status = {
                        "type": "status",
                        "is_active": True,
                        "call_sid": call_sid,
                        "patient_id": patient_id,
                        "duration_sec": duration_sec,
                        "started_at": started_at,
                    }
                    break

            yield f"data: {json.dumps(initial_status)}\n\n"

            # 2. Stream events as they arrive, with periodic heartbeats
            while True:
                try:
                    raw = await asyncio.wait_for(queue.get(), timeout=30.0)
                    event = json.loads(raw)
                    # Only forward events for the requested patient
                    if event.get("patient_id") == patient_id:
                        yield f"data: {raw}\n\n"
                except asyncio.TimeoutError:
                    # Send heartbeat to keep connection alive
                    yield f": heartbeat\n\n"

        except asyncio.CancelledError:
            pass
        finally:
            call_event_bus.unsubscribe(sub_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx/proxy buffering
        },
    )
