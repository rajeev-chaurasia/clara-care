"""
API Routes Module
REST API endpoints for patient data, conversations, wellness digests, and alerts
"""

from .patients import router as patients_router
from .conversations import router as conversations_router
from .wellness import router as wellness_router
from .alerts import router as alerts_router
from .live_status import router as live_status_router
from .call_events import router as call_events_router

# Data insight and report routes
try:
    from .insights import router as insights_router
    from .reports import router as reports_router
    __all__ = [
        "patients_router",
        "conversations_router",
        "wellness_router",
        "alerts_router",
        "live_status_router",
        "call_events_router",
        "insights_router",
        "reports_router"
    ]
except ImportError:
    __all__ = [
        "patients_router",
        "conversations_router",
        "wellness_router",
        "alerts_router",
        "live_status_router",
        "call_events_router"
    ]
