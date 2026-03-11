"""
SenseCritiq synthesis service.
Two-stage LLM pipeline:
  1. Claude Haiku  — filters ~70% of transcript (meta content, filler, facilitator talk)
  2. Claude Sonnet — extracts themes, quotes, findings as structured JSON
"""

import json
import anthropic

client = anthropic.Anthropic()


# ---------------------------------------------------------------------------
# Stage 1 — Haiku pre-screening
# ---------------------------------------------------------------------------

def filter_transcript(transcript_text: str) -> str:
    """
    Remove meta content from a raw diarised transcript.
    Keeps only genuine participant responses and reactions.
    Returns cleaned transcript text.
    """
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8192,
        messages=[
            {
                "role": "user",
                "content": f"""You are preparing a UX research transcript for analysis.

Your task: remove content that is NOT useful for insight extraction, then return the cleaned transcript.

REMOVE:
- Facilitator instructions, prompts, and questions
- Consent form readings and administrative talk
- Greetings, introductions, sign-off pleasantries
- Technical difficulties and off-topic small talk
- Repeated filler (um, uh, you know) — keep one if it signals hesitation

KEEP:
- All participant reactions, opinions, and descriptions
- Moments of confusion, frustration, delight, or surprise
- Any statement that reveals a mental model or expectation
- Quotes that contain "I thought", "I expected", "I didn't know", "I wasn't sure"

Return ONLY the cleaned transcript. Keep the original speaker labels and timestamps.
Do not summarise or paraphrase — preserve verbatim participant speech.

TRANSCRIPT:
{transcript_text}""",
            }
        ],
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# Stage 2 — Sonnet synthesis
# ---------------------------------------------------------------------------

SYNTHESIS_SCHEMA = """
{
  "themes": [
    {
      "id": "theme_001",
      "label": "Short theme name (5 words max)",
      "description": "1-2 sentence description of the pattern",
      "severity": "high | medium | low | positive",
      "quote_count": 0
    }
  ],
  "quotes": [
    {
      "id": "q_001",
      "text": "Verbatim quote — never paraphrase",
      "speaker": "Speaker A",
      "timestamp": "MM:SS",
      "theme_id": "theme_001",
      "theme_label": "Short theme name"
    }
  ],
  "findings": [
    {
      "finding": "One clear, actionable insight backed by evidence",
      "theme_id": "theme_001",
      "supporting_quote": {
        "text": "The single best verbatim quote that proves this finding",
        "speaker": "Speaker A",
        "timestamp": "MM:SS"
      }
    }
  ],
  "participant_count": 0
}
"""


def synthesise(filtered_transcript: str, session_name: str) -> dict:
    """
    Extract themes, quotes, and findings from a filtered transcript.
    Returns a dict matching SYNTHESIS_SCHEMA.
    Raises ValueError if the response cannot be parsed as valid JSON.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        messages=[
            {
                "role": "user",
                "content": f"""You are a senior UX researcher analysing a research session transcript.

Session: {session_name}

Your task: extract structured insights from this transcript.

RULES:
1. Output ONLY valid JSON matching the schema below — no prose, no markdown fences
2. Every finding MUST cite a verbatim quote from the transcript
3. Never invent or paraphrase quotes — copy them exactly as spoken
4. Every quote must have a real timestamp from the transcript
5. Assign each quote to exactly one theme
6. Severity: use "high" for blockers, "medium" for friction, "low" for minor issues, "positive" for delights
7. Aim for 3-6 themes, 1-3 findings per theme, and the 10-20 most insightful quotes

SCHEMA:
{SYNTHESIS_SCHEMA}

TRANSCRIPT:
{filtered_transcript}""",
            }
        ],
    )

    raw = response.content[0].text.strip()

    # Strip markdown code fences if model adds them despite instructions
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Synthesis returned invalid JSON: {e}\nRaw response:\n{raw}")
