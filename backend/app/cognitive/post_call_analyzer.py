"""
Post-Call Analyzer — Gemini + Deepgram Text Intelligence + Elder-Care Keyword Analysis

Uses Gemini LLM (gemini-3-flash-preview) for:
  - Call summary generation (warm, family-friendly, context-aware)
  - Detailed wellness highlights

Uses Deepgram's Text Intelligence API for:
  - Sentiment analysis  
  - Topic detection
  - Intent recognition

Then layers elder-care-specific keyword analysis for:
  - Safety flags (suicidal ideation, falls, self-harm)
  - Medication tracking
  - Loneliness / desire to connect
  - Mood refinement
"""

import json
import logging
import os
import re
from typing import Optional

from .utils import get_pronouns

import httpx

try:
    import google.generativeai as genai
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False

logger = logging.getLogger(__name__)

# ─── Safety keywords (highest priority) ────────────────────────────────────────
# Tier 1: ALWAYS flag — unambiguous crisis language
SAFETY_KEYWORDS_CRITICAL = [
    "suicide", "suicidal", "kill myself", "end my life",
    "don't want to live", "better off dead", "hurt myself",
    "can't go on", "no reason to live", "want to die",
    "overdose", "cut myself", "harm myself", "self harm",
    "end it all",
]

# Tier 2: Context-dependent — may be sarcasm, idioms, or literal
# These get validated against surrounding context before flagging.
SAFETY_KEYWORDS_CONTEXTUAL = [
    "fell", "fall", "fell down", "tripped",
    "jump", "jumped", "jump from", "jump off",
]

# Phrases that NEGATE a contextual keyword (sarcasm, idioms, positive context)
SAFETY_NEGATION_PHRASES = [
    "jumped with joy", "jumped for joy", "fell asleep",
    "fell in love", "fell for", "fall asleep",
    "fall for it", "fell for it", "jump to conclusions",
    "jump at the chance", "jump on it", "jumping for joy",
    "fell over laughing", "fall into place",
    "just kidding", "i'm joking", "joking",
    "sarcastically", "being sarcastic",
    "not really", "no i didn't",
]

# Keep the flat list for backward compat (used by _elder_care_analysis as a reference)
SAFETY_KEYWORDS = SAFETY_KEYWORDS_CRITICAL + SAFETY_KEYWORDS_CONTEXTUAL


# ─── Loneliness indicators ──────────────────────────────────────────────────────
LONELINESS_KEYWORDS = [
    "lonely", "alone", "no one", "nobody", "miss my",
    "wish someone", "all by myself", "no visitors",
    "nobody calls", "nobody visits", "feeling isolated",
    "missing people", "missing family",
]

# ─── Desire to connect (wants to see/talk to family) ───────────────────────────
# IMPORTANT: All patterns with wildcards use [^.!?,]{0,35} instead of .*
# This caps the match to within a single clause and prevents cross-sentence
# false positives (e.g. "miss those days... the whole family was together"
# being misread as a request to see family).
CONNECTION_PHRASES = [
    # Explicit wish/want to have someone visit or meet
    r"wish[^.!?,]{0,35}could come",
    r"wish[^.!?,]{0,35}would visit",
    r"want[^.!?,]{0,35}to (?:come|visit|meet|see) (?:me|us|over)",
    r"come and (?:meet|see) me",
    r"hope[^.!?,]{0,35}(?:calls?|visits?|comes?)",

    # Explicitly missing a specific person (not a vague nostalgic "miss")
    # Must be followed immediately by the relation word within ~4 words
    r"miss(?:ing)?\s+(?:my\s+)?(?:son|daughter|child(?:ren)?|grandchild(?:ren)?|family|kids?)",
    r"miss(?:ing)?\s+(?:you|him|her|them)\b",

    # Wanting to talk to someone
    r"want[^.!?,]{0,35}to talk to[^.!?,]{0,25}(?:you|him|her|them|family|son|daughter)",

    # Direct requests for a visit
    r"want[^.!?,]{0,25}(?:come|visit)[^.!?,]{0,25}over",
    r"wondering if[^.!?,]{0,35}could come",
    r"want (?:him|her|them|you) to come",
    r"family get.?together",
    r"spend[^.!?,]{0,25}time with me",
    r"(?:like|love)[^.!?,]{0,25}to (?:see|visit|meet) (?:you|them|family|everyone)",
]

# NOTE: medication list is no longer hardcoded here.
# It is passed in at call-time from the patient's profile stored in the data store.
# See: twilio_bridge.py → analyze_transcript(transcript, medications=[...])


async def analyze_transcript(
    transcript: str,
    medications: list[str] | None = None,
    patient_context: dict | None = None,
) -> dict:
    """
    Analyze a conversation transcript using Deepgram Text Intelligence
    + elder-care keyword analysis.

    Args:
        transcript:   Full conversation transcript text.
        medications:  List of medication names (lowercase) for this specific
                      patient, sourced from their profile in the data store.
                      Defaults to an empty list — no medications will be
                      tracked if the caller does not provide this.
        patient_context:  Optional dict with patient info for richer analysis:
                          {name, preferred_name, location, family_names, interests}
    """
    patient_meds = [m.lower() for m in (medications or [])]
    ctx = patient_context or {}

    # 1. Deepgram Text Intelligence (summary, sentiment, topics, intents)
    dg_analysis = await _deepgram_analyze(transcript, patient_name=ctx.get("preferred_name", ""))

    # 2. Elder-care keyword analysis (safety, meds, loneliness, connection)
    care_analysis = _elder_care_analysis(transcript, patient_meds, patient_name=ctx.get("name", ""))
    
    # 3. Memory inconsistency detection (YES -> UNSURE -> NO pattern)
    memory_flags = _detect_memory_inconsistency(transcript)
    care_analysis["memory_inconsistency"] = memory_flags
    if memory_flags:
        pname = ctx.get("preferred_name") or ctx.get("name", "").split()[0] if ctx.get("name") else "The patient"
        care_analysis["action_items"].append(
            f"{pname} gave conflicting answers during the call, which may be worth watching."
        )
    
    # 3.5 Generate rich summary via Gemini (replaces Deepgram's generic one)
    gemini_summary, patient_quotes = await _gemini_summarize(transcript, patient_context=ctx)
    if gemini_summary:
        dg_analysis["summary"] = gemini_summary
        logger.info("[POST_CALL] Using Gemini-generated summary instead of Deepgram")

    # 4. Merge into unified result
    result = _merge_analysis(dg_analysis, care_analysis, transcript, patient_context=ctx)
    result["memory_inconsistency"] = memory_flags
    result["patient_quotes"] = patient_quotes  # Attach quotes for use in alerts/digests
    
    logger.info(
        f"[POST_CALL_ANALYSIS] mood={result.get('mood')}, "
        f"topics={result.get('topics')}, "
        f"safety_flags={len(result.get('safety_flags', []))}, "
        f"desire_to_connect={result.get('desire_to_connect')}, "
        f"memory_flags={len(memory_flags)}"
    )
    
    return result


async def _deepgram_analyze(transcript: str, patient_name: str = "") -> dict:
    """
    Call Deepgram's /v1/read endpoint for text intelligence.
    NOTE: Deepgram is used ONLY for sentiment, topics, and intents.
    Summarization is handled by Gemini for higher quality.
    """
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        logger.warning("DEEPGRAM_API_KEY not set — skipping Deepgram analysis")
        return {}
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.deepgram.com/v1/read",
                params={
                    "sentiment": "true",
                    "topics": "true",
                    "intents": "true",
                    "language": "en",
                },
                headers={
                    "Authorization": f"Token {api_key}",
                    "Content-Type": "application/json",
                },
                json={"text": transcript},
            )
            response.raise_for_status()
            data = response.json()
            
            results = data.get("results", {})
            
            logger.debug(
                "[DEEPGRAM_RAW] %s",
                json.dumps(results, indent=2, default=str)[:4000]
            )
            
            # ── Extract topics ──────────────────────────────────────────
            topics_data = results.get("topics", {}).get("segments", [])
            topics: list[str] = []
            for seg in topics_data:
                for topic in seg.get("topics", []):
                    t = topic.get("topic", "")
                    if t and t not in topics:
                        topics.append(t)
            
            # ── Extract sentiment ───────────────────────────────────────
            sentiments_data = results.get("sentiments", {}).get("average", {})
            sentiment = sentiments_data.get("sentiment", "neutral")
            sentiment_score = sentiments_data.get("sentiment_score", 0)
            
            # ── Extract intents ─────────────────────────────────────────
            intents_data = results.get("intents", {}).get("segments", [])
            intents: list[str] = []
            for seg in intents_data:
                for intent in seg.get("intents", []):
                    i = intent.get("intent", "")
                    if i and i not in intents:
                        intents.append(i)
            
            logger.info(
                f"[DEEPGRAM_INTEL] "
                f"topics={topics}, sentiment={sentiment}({sentiment_score:.2f}), "
                f"intents={intents}"
            )
            
            return {
                "topics": topics,
                "sentiment": sentiment,
                "sentiment_score": sentiment_score,
                "intents": intents,
            }
            
    except Exception as e:
        logger.error(f"Deepgram text intelligence failed: {e}")
        return {}


def _elder_care_analysis(transcript: str, medications: list[str], patient_name: str = "") -> dict:
    """
    Elder-care-specific keyword analysis for signals Deepgram
    doesn't natively detect (safety, meds, loneliness, connection).

    Args:
        medications: Lowercase medication names from the patient's profile.
    """
    patient_text = _extract_patient_text(transcript)
    patient_lower = patient_text.lower()
    
    # Safety flags
    safety_flags = _scan_safety_keywords(transcript)
    
    # Loneliness indicators
    loneliness = []
    for keyword in LONELINESS_KEYWORDS:
        if keyword in patient_lower:
            idx = patient_lower.index(keyword)
            start = max(0, idx - 30)
            end = min(len(patient_text), idx + len(keyword) + 50)
            context = patient_text[start:end].strip()
            loneliness.append(context)
    
    # Desire to connect
    desire_to_connect = False
    connection_context = ""
    for pattern in CONNECTION_PHRASES:
        match = re.search(pattern, patient_lower)
        if match:
            desire_to_connect = True
            idx = match.start()
            start = max(0, idx - 20)
            end = min(len(patient_text), match.end() + 40)
            connection_context = patient_text[start:end].strip()
            break
    
    # Medication tracking — uses this patient's specific medication list
    medication_status = _extract_medication_status(transcript, medications, patient_name=patient_name)
    
    # Action items from conversation
    action_items = []
    if medication_status.get("medications_mentioned"):
        missed = [m["name"] for m in medication_status["medications_mentioned"] if m.get("taken") is False]
        if missed:
            action_items.append(f"Missed medication: {', '.join(missed)}")
    if desire_to_connect:
        action_items.append(f"Wants family connection: {connection_context}")
    if loneliness:
        action_items.append("Expressed feelings of loneliness")
    
    return {
        "safety_flags": safety_flags,
        "loneliness_indicators": loneliness,
        "desire_to_connect": desire_to_connect,
        "connection_context": connection_context,
        "medication_status": medication_status,
        "action_items": action_items,
    }


async def _gemini_summarize(transcript: str, patient_context: dict | None = None) -> tuple[str, list[str]]:
    """
    Generate a warm, context-aware summary using Gemini LLM.
    Returns tuple of (summary, patient_quotes).
    Returns ("", []) if Gemini is unavailable or fails.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or not _GEMINI_AVAILABLE:
        logger.info("[GEMINI] Skipping — no API key or google-generativeai not installed")
        return "", []

    ctx = patient_context or {}
    pname = ctx.get("preferred_name") or ctx.get("name", "").split()[0] if ctx.get("name") else "the patient"
    family_names = ctx.get("family_names", [])
    interests = ctx.get("interests", [])
    location = ctx.get("location", "")

    context_block = f"Patient's name: {pname}"
    if family_names:
        context_block += f"\nFamily members: {', '.join(family_names)}"
    if interests:
        context_block += f"\nKnown interests: {', '.join(interests)}"
    if location:
        context_block += f"\nLocation: {location}"

    prompt = f"""You are summarizing a phone conversation between Clara (an AI companion) and {pname}, an elderly person who receives daily wellness check-in calls.

PATIENT CONTEXT:
{context_block}

TRANSCRIPT:
{transcript}

Write a warm, natural summary in 4-5 sentences for {pname}'s family members to read on their dashboard. Rules:
- Write in third person about {pname} (e.g. "{pname} chatted about...")
- Focus on WHAT they talked about (topics, stories, requests) and HOW {pname} seemed (mood, energy)
- Use warm, human language — this is for worried family members, not clinicians
- Do NOT mention "Clara", "AI", "companion", "agent", "the caller", or "the host"
- Do NOT use clinical language, jargon, or bullet points
- Keep it under 100 words
- If {pname} mentioned wanting to talk to family, highlight that

After the summary, add a new line with exactly this format:
QUOTES: "quote one" | "quote two" | "quote three"
Pick 2-3 short, memorable things {pname} actually said during the call — funny, sweet, or revealing moments that would make the family smile. Use their exact words from the transcript. If nothing stands out, write QUOTES: none"""

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-3-flash-preview")
        response = model.generate_content(prompt)
        raw = response.text.strip().strip('"').strip("'").strip()
        
        # Parse out quotes if present
        quotes = []
        if "QUOTES:" in raw:
            parts = raw.split("QUOTES:", 1)
            summary = parts[0].strip()
            quotes_raw = parts[1].strip()
            if quotes_raw.lower() != "none":
                quotes = [q.strip().strip('"').strip("'") for q in quotes_raw.split("|") if q.strip()]
        else:
            summary = raw
        
        logger.info(f"[GEMINI] Generated summary ({len(summary)} chars), {len(quotes)} quotes")
        return summary, quotes
    except Exception as exc:
        logger.warning(f"[GEMINI] Summary generation failed: {exc}")
        return "", []


def _merge_analysis(dg: dict, care: dict, transcript: str, patient_context: dict | None = None) -> dict:
    """Merge Deepgram intelligence with elder-care analysis into unified result."""
    ctx = patient_context or {}
    pname = ctx.get("preferred_name") or ctx.get("name", "").split()[0] if ctx.get("name") else "The patient"
    p = get_pronouns(pname)
    
    # Summary: Gemini LLM generates this (injected into dg dict in analyze_transcript)
    summary = dg.get("summary", "")
    if not summary:
        # Minimal fallback if Gemini was unavailable
        summary = f"{pname} had a check-in call today."
    
    # Mood: map Deepgram sentiment to our mood categories, override if safety/loneliness
    safety_flags = care.get("safety_flags", [])
    loneliness = care.get("loneliness_indicators", [])
    
    if safety_flags:
        mood = "distressed"
        mood_explanation = (
            f"{p['Sub']} said something during the call that raises a safety concern and needs your attention right away."
        )
    elif loneliness:
        mood = "sad"
        mood_explanation = f"{p['Sub']} expressed feelings of loneliness or missing people {p['sub']} loves."
    else:
        dg_sentiment = dg.get("sentiment", "neutral")
        mood_map = {
            "positive": "happy",
            "negative": "sad", 
            "neutral": "neutral",
        }
        mood = mood_map.get(dg_sentiment, "neutral")
        score = dg.get("sentiment_score", 0)
        mood_map_label = {"positive": "upbeat and positive", "negative": "low or subdued", "neutral": "calm and neutral"}
        mood_explanation = f"{p['Pos']} overall tone during the call felt {mood_map_label.get(dg_sentiment, 'neutral')}."
    
    # Topics: trust Deepgram's semantic topics, then layer in our
    # keyword-detected elder-care signals that Deepgram can't catch
    topics = list(dg.get("topics", []))  # copy
    topics_lower = [t.lower() for t in topics]
    if loneliness and "loneliness" not in topics_lower:
        topics.append("loneliness")
    if care.get("desire_to_connect") and "family" not in topics_lower:
        topics.append("family connection")
    if care.get("medication_status", {}).get("discussed"):
        if "medication" not in topics_lower:
            topics.append("medication")
    
    # Action items: keyword-based elder-care items + Deepgram intents
    action_items = list(care.get("action_items", []))
    for intent in dg.get("intents", []):
        item = f"{p['Sub']} expressed a {intent.lower()} during the conversation"
        if item not in action_items:
            action_items.append(item)
    
    # Engagement level from transcript length
    patient_text = _extract_patient_text(transcript)
    word_count = len(patient_text.split())
    if word_count > 100:
        engagement = "high"
    elif word_count > 40:
        engagement = "medium"
    else:
        engagement = "low"
    
    return {
        "summary": summary,
        "mood": mood,
        "mood_explanation": mood_explanation,
        "topics": topics,
        "action_items": action_items,
        "medication_status": care.get("medication_status", {"discussed": False, "medications_mentioned": [], "notes": ""}),
        "safety_flags": safety_flags,
        "engagement_level": engagement,
        "loneliness_indicators": loneliness,
        "wants_to_talk_about": topics[:5],
        "notable_requests": action_items,
        "desire_to_connect": care.get("desire_to_connect", False),
        "connection_context": care.get("connection_context", ""),
    }



# ─── Helpers ────────────────────────────────────────────────────────────────────


def _extract_patient_text(transcript: str) -> str:
    """Extract all patient-side utterances from transcript."""
    lines = transcript.split("\n")
    patient_msgs = []
    for line in lines:
        line = line.strip()
        if line.startswith("Patient:"):
            patient_msgs.append(line.split(":", 1)[1].strip())
    return " ".join(patient_msgs)


def _detect_memory_inconsistency(transcript: str) -> list[str]:
    """
    Detect memory inconsistency patterns within patient turns.
    Catches YES -> UNSURE -> NO contradictions within a sliding window of turns,
    but only for substantial contradictions (not conversational fillers like 'No. I'm doing good').
    """
    lines = transcript.split("\n")
    patient_turns = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        speaker = line.split(":", 1)[0].strip().lower()
        if speaker in ("patient", "emily", "dorothy"):
            text = line.split(":", 1)[1].strip().lower() if ":" in line else ""
            # Only include turns with enough substance (>3 words) to avoid fillers
            if len(text.split()) > 3:
                patient_turns.append(text)
    
    if len(patient_turns) < 3:
        return []
    
    flags = []
    # Require longer, more specific phrases to reduce false positives
    affirmative = {"yes i did", "yes i took", "i did take", "of course i did", "i remember"}
    uncertain = {"i think so", "maybe i did", "probably", "not sure if", "i guess so", "i can't recall"}
    negative = {"can't remember", "i don't know", "i forgot", "didn't take", "haven't done", "i don't remember"}
    
    # Sliding window of 3-4 turns — must have same topic context
    window_size = 4
    for i in range(len(patient_turns) - 2):
        window = patient_turns[i:i + window_size]
        
        has_affirm = any(any(a in turn for a in affirmative) for turn in window[:2])
        has_contradict = any(
            any(n in turn for n in negative) or any(u in turn for u in uncertain)
            for turn in window[2:]
        )
        
        if has_affirm and has_contradict:
            context = " → ".join(f'"{t[:50]}"' for t in window if t)
            flags.append(
                f"Patient changed from affirmative to uncertain/negative within {len(window)} turns: {context}"
            )
            break  # One flag per conversation is enough
    
    return flags


def _scan_safety_keywords(transcript: str) -> list[str]:
    """
    Scan transcript for critical safety keywords.
    Tier 1 (crisis) keywords always flag.
    Tier 2 (contextual) keywords are checked against negation phrases
    to prevent false positives from sarcasm and idioms.
    Only flags patient speech, not Clara's.
    """
    # Extract only patient-side text
    patient_text = _extract_patient_text(transcript)
    text_lower = patient_text.lower()
    flags = []

    # Tier 1: Always flag (unambiguous crisis language)
    for keyword in SAFETY_KEYWORDS_CRITICAL:
        if keyword in text_lower:
            idx = text_lower.index(keyword)
            start = max(0, idx - 50)
            end = min(len(patient_text), idx + len(keyword) + 50)
            context = patient_text[start:end].strip()
            flags.append(f"Safety keyword '{keyword}': \"{context}\"")

    # Tier 2: Context-dependent flags
    for keyword in SAFETY_KEYWORDS_CONTEXTUAL:
        if keyword in text_lower:
            idx = text_lower.index(keyword)
            # Extract surrounding context (100 chars each side)
            start = max(0, idx - 100)
            end = min(len(patient_text), idx + len(keyword) + 100)
            surrounding = patient_text[start:end].lower()

            # Skip if a negation phrase is found in the surrounding context
            if any(neg in surrounding for neg in SAFETY_NEGATION_PHRASES):
                logger.info(
                    f"[SAFETY_SKIP] Contextual keyword '{keyword}' negated by context"
                )
                continue

            context = patient_text[start:end].strip()
            flags.append(f"Safety keyword '{keyword}': \"{context}\"")

    return flags


def _extract_medication_status(transcript: str, medications: list[str], patient_name: str = "") -> dict:
    """
    Extract medication mentions and whether they were taken.

    Strategy: scan EVERY mention of each medication in the transcript, track
    the signals found (positive / negative / indirect), and resolve using the
    LAST clear signal.  This handles the common "No… actually my pill box is
    empty" correction pattern without misclassifying it as 'not taken'.

    Args:
        medications: Lowercase medication names from the patient's profile.
                     An empty list means no medications are tracked.
    """
    text_lower = transcript.lower()
    meds_mentioned = []
    discussed = False

    # Positive signals — direct confirmation or indirect evidence
    _TAKEN_WORDS = [
        "took", "taken", "had my", "just took", "already took",
        "yes", "yeah", "yep", "i did", "i have",
    ]
    # Indirect / inferred confirmation (e.g. empty pill box means it was taken)
    _INDIRECT_TAKEN = [
        "pill box is empty", "pillbox is empty", "pill box empty",
        "pillbox empty", "it's empty", "its empty",
        "i must have taken", "must have taken", "already done",
    ]
    # Negative signals
    _MISSED_WORDS = [
        "missed", "forgot", "didn't take", "haven't taken",
        "not yet", "not taken", "no,", "no i", "no,\n",
    ]

    CONTEXT_WINDOW = 100  # chars on each side of a mention to check

    for med in medications:
        if med not in text_lower:
            continue

        discussed = True
        # Collect every start position where this medication appears
        positions = []
        start = 0
        while True:
            idx = text_lower.find(med, start)
            if idx == -1:
                break
            positions.append(idx)
            start = idx + 1

        # Evaluate signal at each position; keep a running 'last_signal'
        last_signal: bool | None = None
        was_corrected = False

        for idx in positions:
            ctx_start = max(0, idx - CONTEXT_WINDOW)
            ctx_end = min(len(text_lower), idx + len(med) + CONTEXT_WINDOW)
            ctx = text_lower[ctx_start:ctx_end]

            is_positive  = any(w in ctx for w in _TAKEN_WORDS)
            is_indirect  = any(w in text_lower for w in _INDIRECT_TAKEN)  # whole transcript
            is_negative  = any(w in ctx for w in _MISSED_WORDS)

            if is_indirect or is_positive:
                if last_signal is False:
                    was_corrected = True   # denial was reversed
                last_signal = True
            elif is_negative:
                last_signal = False
            # If neither, leave last_signal unchanged (ambiguous mention)

        taken = last_signal  # None if no clear signal at all

        # Build a human-readable note for this medication
        med_name = med.title()
        p = get_pronouns(patient_name)
        if taken is True and was_corrected:
            note = f"{p['Sub']} initially said no to {med_name} but later confirmed {p['sub']} took it (pill box was empty)."
        elif taken is True:
            note = f"{p['Sub']} confirmed {p['sub']} took {p['pos']} {med_name}."
        elif taken is False:
            note = f"{p['Sub']} said {p['sub']} did not take {p['pos']} {med_name}."
        else:
            note = f"{p['Sub']} mentioned {med_name} but didn't clearly confirm whether {p['sub']} took it."

        meds_mentioned.append({"name": med_name, "taken": taken, "note": note})

    # Aggregate notes
    notes = " ".join(m["note"] for m in meds_mentioned) if meds_mentioned else ""

    return {
        "discussed": discussed,
        "medications_mentioned": meds_mentioned,
        "notes": notes.strip(),
    }

