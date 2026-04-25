"""
ClaraCare FastAPI Application
Main entry point for the backend server
"""

import logging
import os
from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

# Load environment variables from .env file
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

from .voice import twilio_bridge, session_manager, outbound_manager

# Cognitive analysis and storage components
from .storage import InMemoryDataStore, SanityDataStore
from .cognitive.analyzer import CognitiveAnalyzer
from .cognitive.baseline import BaselineTracker
from .cognitive.alerts import AlertEngine
from .cognitive.pipeline import CognitivePipeline
from .notifications.email import EmailNotifier
from .routes import (
    patients_router,
    conversations_router,
    wellness_router,
    alerts_router,
    live_status_router,
    call_events_router
)
from .routes import patients, conversations, wellness, alerts

# Data insights, reports, and nostalgia routes
try:
    from .routes.insights import router as insights_router
    from .routes.reports import router as reports_router
    HAS_DATA_ROUTES = True
except ImportError as e:
    HAS_DATA_ROUTES = False
    # logger not available yet at module level
    import sys
    print(f"Warning: Optional routes not available: {e}", file=sys.stderr)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan context manager
    Handles startup and shutdown
    """
    # Startup
    logger.info("Starting ClaraCare backend...")
    
    # Initialize cognitive analysis components
    logger.info("Initializing cognitive analysis system...")
    
    # Decide between Sanity and in-memory storage based on available credentials
    sanity_project_id = os.getenv("SANITY_PROJECT_ID")
    sanity_dataset = os.getenv("SANITY_DATASET")
    sanity_token = os.getenv("SANITY_TOKEN")
    
    if sanity_project_id and sanity_dataset and sanity_token:
        logger.info("✓ Sanity credentials found - using SanityDataStore")
        data_store = SanityDataStore(
            project_id=sanity_project_id,
            dataset=sanity_dataset,
            token=sanity_token
        )
    else:
        logger.info("⚠ Sanity credentials not found - using InMemoryDataStore (testing mode)")
        logger.info("  To use Sanity, set SANITY_PROJECT_ID, SANITY_DATASET, and SANITY_TOKEN")
        data_store = InMemoryDataStore()
    
    # Cognitive components
    analyzer = CognitiveAnalyzer()
    baseline_tracker = BaselineTracker(data_store)
    
    # Notification service
    notification_service = EmailNotifier(data_store=data_store)
    
    # Alert engine
    alert_engine = AlertEngine(data_store, notification_service)
    
    # Cognitive pipeline orchestrator
    cognitive_pipeline = CognitivePipeline(
        analyzer=analyzer,
        baseline_tracker=baseline_tracker,
        alert_engine=alert_engine,
        data_store=data_store,
        notification_service=notification_service
    )
    
    # Store in app state for access in routes (via app.dependencies.get_data_store)
    app.state.data_store = data_store
    app.state.cognitive_pipeline = cognitive_pipeline
    
    # Set data store in insights and reports routes if available
    if HAS_DATA_ROUTES:
        from .routes import reports
        from .reports.generator import ReportGenerator
        from .reports.foxit_client import FoxitClient, FoxitPDFServicesClient
        
        # Initialize reports with report generator + PDF Services
        foxit_client = FoxitClient()
        pdf_services = FoxitPDFServicesClient()
        report_gen = ReportGenerator(data_store, foxit_client, pdf_services)
        reports.set_report_generator(report_gen)
        
        logger.info("✓ Data routes initialized")
    
    # Set cognitive pipeline in Twilio bridge for real-time analysis
    twilio_bridge.set_cognitive_pipeline(cognitive_pipeline)
    
    logger.info("Cognitive analysis system initialized ✓")
    
    yield
    
    # Shutdown
    logger.info("Shutting down ClaraCare backend...")
    
    # Cleanup Sanity client if using SanityDataStore
    if isinstance(data_store, SanityDataStore):
        await data_store.close()


# Create FastAPI app
app = FastAPI(
    title="ClaraCare API",
    description="AI Elder Care Companion - Voice Agent Backend",
    version="1.0.0",
    lifespan=lifespan
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API routers
app.include_router(patients_router)
app.include_router(conversations_router)
app.include_router(wellness_router)
app.include_router(alerts_router)
app.include_router(live_status_router)
app.include_router(call_events_router)

# Register data routes if available
if HAS_DATA_ROUTES:
    app.include_router(insights_router)
    app.include_router(reports_router)
    logger.info("✓ Data routes registered")


@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "service": "ClaraCare Backend",
        "status": "running",
        "version": "1.0.0"
    }


@app.get("/health")
async def health_check():
    """Detailed health check"""
    return {
        "status": "healthy",
        "active_calls": twilio_bridge.get_active_call_count(),
        "active_sessions": len(session_manager.sessions)
    }


@app.get("/dev/status")
async def dev_status():
    """
    Developer diagnostic endpoint
    Shows system health, active sessions, Twilio config, and pipeline readiness.
    Use this to verify the system is properly configured before making a call.
    """
    from .voice.outbound import outbound_manager
    
    # Check Twilio config
    twilio_ok = bool(
        outbound_manager.account_sid 
        and outbound_manager.auth_token 
        and outbound_manager.from_number
    )
    
    # Check Deepgram config
    deepgram_key = os.getenv("DEEPGRAM_API_KEY", "")
    deepgram_ok = bool(deepgram_key and len(deepgram_key) > 10)
    
    # Check cognitive pipeline
    pipeline_ready = twilio_bridge.cognitive_pipeline is not None
    
    # Active call details
    active_calls = []
    for call_sid, session in twilio_bridge.active_calls.items():
        duration = 0
        if session.call_start_time:
            from datetime import datetime, UTC
            duration = int((datetime.now(UTC) - session.call_start_time).total_seconds())
        active_calls.append({
            "call_sid": call_sid,
            "patient_id": session.patient_id,
            "is_active": session.is_active,
            "duration_sec": duration,
            "transcript_turns": len(session.conversation_transcript),
        })
    
    return {
        "system": "claracare-backend",
        "status": "ready" if (twilio_ok and deepgram_ok) else "misconfigured",
        "config": {
            "twilio": {
                "configured": twilio_ok,
                "phone_number": outbound_manager.from_number or "NOT_SET",
                "server_url": outbound_manager.server_url,
            },
            "deepgram": {
                "configured": deepgram_ok,
            },
            "cognitive_pipeline": {
                "ready": pipeline_ready,
            },
        },
        "calls": {
            "active_count": twilio_bridge.get_active_call_count(),
            "agent_sessions": len(session_manager.sessions),
            "active_calls": active_calls,
        },
    }


@app.get("/voice/twiml")
async def twiml_handler(patient_id: str = "demo-patient"):
    """
    TwiML endpoint for outbound calls
    Returns TwiML that connects the call to our WebSocket
    
    Query params:
        patient_id: Patient identifier
    """
    server_url = os.getenv("SERVER_PUBLIC_URL", "http://localhost:8000")
    
    # Strip protocol to get hostname for WSS URL
    ws_host = server_url.replace('https://', '').replace('http://', '')
    
    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{ws_host}/voice/twilio">
            <Parameter name="patient_id" value="{patient_id}" />
        </Stream>
    </Connect>
</Response>'''
    
    return Response(content=twiml, media_type="application/xml")


@app.post("/voice/status")
async def call_status_callback(request: Request):
    """
    Twilio call status callback
    Receives updates about call status (initiated, ringing, answered, completed)
    """
    form_data = await request.form()
    call_sid = form_data.get("CallSid")
    call_status = form_data.get("CallStatus")
    call_duration = form_data.get("CallDuration", "N/A")
    direction = form_data.get("Direction", "unknown")
    from_number = form_data.get("From", "")
    to_number = form_data.get("To", "")
    
    logger.info(
        f"[TWILIO_STATUS] CallSid={call_sid} status={call_status} "
        f"duration={call_duration}s direction={direction} "
        f"from={from_number} to={to_number}"
    )
    
    return {"status": "received"}


@app.websocket("/voice/twilio")
async def twilio_websocket(websocket: WebSocket, patient_id: str = "demo-patient"):
    """
    Twilio Media Stream WebSocket endpoint
    
    Query params:
        patient_id: Patient identifier (default: demo-patient)
    
    Example Twilio webhook URL:
        wss://your-domain.com/voice/twilio?patient_id=patient-123
    """
    logger.info(f"Incoming Twilio call for patient: {patient_id}")
    
    await twilio_bridge.handle_call(
        websocket=websocket,
        patient_id=patient_id
    )


@app.post("/voice/call/end/{call_sid}")
async def end_call(call_sid: str):
    """
    Manually end an active call
    
    Args:
        call_sid: Twilio call SID
    """
    try:
        await twilio_bridge.end_call(call_sid)
        return {"message": f"Call {call_sid} ended"}
    except Exception as e:
        logger.error(f"Error ending call: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/voice/call/patient")
async def initiate_call_to_patient(request: Request):
    """
    Initiate an outbound call to a patient
    
    Request Body (JSON):
        patient_id: Patient identifier
        patient_phone: Patient's phone number (E.164 format: +1234567890)
        patient_name: Patient's name (optional)
        
    Example:
        POST /voice/call/patient
        {
            "patient_id": "patient-123",
            "patient_phone": "+11234567890",
            "patient_name": "Margaret"
        }
    """
    body = await request.json()
    patient_id = body.get("patient_id")
    patient_phone = body.get("patient_phone")
    patient_name = body.get("patient_name", "Patient")
    
    if not patient_id or not patient_phone:
        raise HTTPException(status_code=400, detail="patient_id and patient_phone are required")
    
    logger.info(f"Initiating call to {patient_name} ({patient_phone})")
    
    result = await outbound_manager.call_patient(
        patient_id=patient_id,
        patient_phone=patient_phone,
        patient_name=patient_name
    )
    
    if result.get("success"):
        return result
    else:
        raise HTTPException(status_code=500, detail=result.get("error", "Call failed"))


@app.post("/voice/call/daily-checkins")
async def trigger_daily_checkins():
    """
    Trigger daily check-in calls for all patients
    
    In production, this would:
    1. Fetch patient list from Sanity
    2. Filter patients who need check-ins today
    3. Initiate calls to each patient
    """
    # Example patient list (in production, fetch from Sanity)
    demo_patients = [
        {
            "patient_id": "demo-patient",
            "phone": "+11234567890",  # Replace with real number for testing
            "name": "Demo Patient"
        }
    ]
    
    logger.info("Triggering daily check-ins...")
    
    result = await outbound_manager.call_multiple_patients(demo_patients)
    
    return result


@app.get("/voice/calls")
async def list_active_calls():
    """List all active calls"""
    calls = []
    for call_sid, session in twilio_bridge.active_calls.items():
        calls.append({
            "call_sid": call_sid,
            "patient_id": session.patient_id,
            "is_active": session.is_active,
            "stream_sid": session.twilio_stream.stream_sid
        })
    
    return {
        "active_calls": calls,
        "count": len(calls)
    }


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler"""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "detail": str(exc)
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=os.getenv("ENV", "production") != "production",
        log_level="info"
    )
