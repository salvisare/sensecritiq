"""
SenseCritiq — Real session endpoints.
Replaces stub implementations for search and report generation.

Registered first in main.py so it takes precedence over stubs.py for
matching routes (/v1/search, /v1/sessions/{id}/report).
"""

import io
import json
import os
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import boto3
from databases import Database
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter()


# ── Auth (reuse from chat.py) ─────────────────────────────────────────────────
async def get_account_id(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    token = auth_header[len("Bearer "):]
    if not token.startswith("scq_"):
        from api.portal import _verify_clerk_token
        try:
            user_info = await _verify_clerk_token(request)
            clerk_user_id = user_info.get("clerk_user_id", "")
            email = user_info.get("email", "")
            db: Database = request.app.state.db
            row = await db.fetch_one(
                "SELECT id FROM accounts WHERE clerk_user_id = :cuid OR email = :email LIMIT 1",
                {"cuid": clerk_user_id, "email": email},
            )
            if not row:
                raise HTTPException(status_code=401, detail="Account not found")
            return str(row["id"])
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
    from middleware.auth import verify_api_key
    return await verify_api_key(request)


# ── POST /v1/search ───────────────────────────────────────────────────────────
@router.post("/search")
async def search_research(
    request: Request,
    account_id: str = Depends(get_account_id),
):
    """
    Full-text ILIKE search across quotes and session names.
    Mounted at /v1/search via include_router(prefix='/v1').

    Falls back gracefully when pgvector is unavailable.
    Searches:
    1. quotes.text ILIKE '%query%'        — verbatim quote content
    2. sessions.name / findings ILIKE     — session-level match
    Returns deduplicated results ranked by match type.
    """
    body = await request.json()
    query: str = body.get("query", "").strip()
    project_id: Optional[str] = body.get("project_id")
    date_from: Optional[str] = body.get("date_from")
    date_to: Optional[str] = body.get("date_to")

    if not query:
        return {"query": query, "results": [], "total": 0}

    db: Database = request.app.state.db
    pattern = f"%{query}%"
    results = []

    # ── 1. Search quotes table ────────────────────────────────────────────
    try:
        q = """
            SELECT
                qt.id AS quote_id,
                qt.text,
                qt.speaker,
                qt.timestamp_sec,
                qt.theme_label,
                s.id   AS session_id,
                s.name AS session_name,
                s.created_at
            FROM quotes qt
            JOIN sessions s ON s.id = qt.session_id
            WHERE qt.account_id = :aid
              AND qt.text ILIKE :pattern
        """
        params: dict = {"aid": account_id, "pattern": pattern}

        if project_id:
            q += " AND s.project_id = :pid"
            params["pid"] = project_id
        if date_from:
            q += " AND s.created_at >= :dfrom"
            params["dfrom"] = date_from
        if date_to:
            q += " AND s.created_at <= :dto"
            params["dto"] = date_to

        q += " ORDER BY s.created_at DESC LIMIT 20"
        rows = await db.fetch_all(q, params)

        for r in rows:
            ts = r["timestamp_sec"]
            timestamp_str = None
            if ts is not None:
                h, remainder = divmod(ts, 3600)
                m, s_ = divmod(remainder, 60)
                timestamp_str = f"{h:02d}:{m:02d}:{s_:02d}"

            results.append({
                "relevance_score": 0.90,
                "quote": r["text"],
                "speaker": r["speaker"],
                "timestamp": timestamp_str,
                "session_id": str(r["session_id"]),
                "session_name": r["session_name"],
                "date": str(r["created_at"])[:10] if r["created_at"] else None,
                "theme": r["theme_label"],
                "synthesis_summary": None,
            })
    except Exception as e:
        print(f"[search] quotes query failed: {e}")

    # ── 2. Search session names (add sessions without quote hits) ─────────
    try:
        sq = """
            SELECT id, name, themes, findings, created_at
            FROM sessions
            WHERE account_id = :aid
              AND (name ILIKE :pattern
                   OR themes::text ILIKE :pattern
                   OR findings::text ILIKE :pattern)
        """
        sparams: dict = {"aid": account_id, "pattern": pattern}
        if date_from:
            sq += " AND created_at >= :dfrom"
            sparams["dfrom"] = date_from
        if date_to:
            sq += " AND created_at <= :dto"
            sparams["dto"] = date_to
        sq += " ORDER BY created_at DESC LIMIT 10"
        srows = await db.fetch_all(sq, sparams)

        seen_sessions = {r["session_id"] for r in results}
        for r in srows:
            sid = str(r["id"])
            if sid in seen_sessions:
                continue
            # Pull a representative quote from this session
            findings = r["findings"]
            if isinstance(findings, str):
                try:
                    findings = json.loads(findings)
                except Exception:
                    findings = []
            first_quote = ""
            if findings and isinstance(findings, list):
                sq_obj = findings[0].get("supporting_quote", {}) if findings else {}
                first_quote = sq_obj.get("text", "") if sq_obj else ""

            results.append({
                "relevance_score": 0.70,
                "quote": first_quote,
                "speaker": None,
                "timestamp": None,
                "session_id": sid,
                "session_name": r["name"],
                "date": str(r["created_at"])[:10] if r["created_at"] else None,
                "theme": None,
                "synthesis_summary": f"Session matched query: '{query}'",
            })
    except Exception as e:
        print(f"[search] sessions query failed: {e}")

    # Sort by relevance score desc
    results.sort(key=lambda x: x["relevance_score"], reverse=True)

    return {"query": query, "results": results, "total": len(results)}


# ── POST /v1/sessions/{session_id}/report ─────────────────────────────────────
@router.post("/sessions/{session_id}/report")
async def generate_report(
    session_id: str,
    request: Request,
    account_id: str = Depends(get_account_id),
):
    """
    Generate a Markdown or PDF report for a completed session.
    Uploads the report to R2 and returns a pre-signed URL (1 hour TTL).
    """
    body = await request.json()
    fmt: str = body.get("format", "markdown")

    db: Database = request.app.state.db

    # Fetch session
    row = await db.fetch_one(
        """SELECT id, name, status, themes, findings, quote_count
           FROM sessions WHERE id = :id AND account_id = :aid""",
        {"id": session_id, "aid": account_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    if row["status"] != "ready":
        raise HTTPException(
            status_code=400,
            detail=f"Session is not ready (status: {row['status']})"
        )

    # Parse stored JSON
    themes = row["themes"] or []
    findings = row["findings"] or []
    if isinstance(themes, str):
        try:
            themes = json.loads(themes)
        except Exception:
            themes = []
    if isinstance(findings, str):
        try:
            findings = json.loads(findings)
        except Exception:
            findings = []

    # Fetch quotes
    quote_rows = await db.fetch_all(
        """SELECT text, speaker, timestamp_sec, theme_label
           FROM quotes WHERE session_id = :sid ORDER BY timestamp_sec""",
        {"sid": session_id},
    )
    quotes = []
    for q in quote_rows:
        ts = q["timestamp_sec"]
        ts_str = None
        if ts is not None:
            h, remainder = divmod(ts, 3600)
            m, s_ = divmod(remainder, 60)
            ts_str = f"{h:02d}:{m:02d}:{s_:02d}"
        quotes.append({
            "text": q["text"],
            "speaker": q["speaker"],
            "timestamp": ts_str,
            "theme": q["theme_label"] or "Other",
        })

    data = {
        "themes": themes,
        "findings": findings,
        "quotes": quotes,
        "participant_count": 0,  # not tracked yet
    }

    session_name = row["name"] or "Research Session"

    # Generate report content
    if fmt == "pdf":
        report_bytes, ext, content_type = _build_pdf(session_id, session_name, data)
    else:
        md_text = _build_markdown(session_id, session_name, data)
        report_bytes = md_text.encode("utf-8")
        ext = "md"
        content_type = "text/markdown"

    # Upload to R2
    report_key = f"reports/{account_id}/{session_id}.{ext}"
    try:
        from botocore.config import Config as BotoConfig
        s3 = boto3.client(
            "s3",
            endpoint_url=os.environ["R2_ENDPOINT_URL"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            config=BotoConfig(signature_version="s3v4"),
        )
        bucket = os.environ["R2_BUCKET_NAME"]
        s3.put_object(
            Bucket=bucket,
            Key=report_key,
            Body=report_bytes,
            ContentType=content_type,
        )
        download_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": report_key},
            ExpiresIn=3600,
        )
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    except Exception as e:
        print(f"[report] R2 upload failed: {e}")
        # Fallback: return report content inline as base64 for markdown
        import base64
        download_url = f"data:{content_type};base64," + base64.b64encode(report_bytes).decode()
        expires_at = None

    return {
        "session_id": session_id,
        "format": fmt,
        "download_url": download_url,
        "expires_at": expires_at,
    }


# ── Report renderers ──────────────────────────────────────────────────────────

def _build_markdown(session_id: str, session_name: str, data: dict) -> str:
    """Render synthesis data as a Markdown report."""
    themes = data.get("themes", [])
    quotes = data.get("quotes", [])
    findings = data.get("findings", [])
    generated_at = datetime.now(timezone.utc).strftime("%B %d, %Y")

    lines = [
        f"# {session_name}",
        f"**Generated:** {generated_at}  ",
        f"**Session ID:** `{session_id}`  ",
        f"**Quotes captured:** {len(quotes)}",
        "",
        "---",
        "",
        "## Themes",
        "",
    ]

    for i, theme in enumerate(themes, 1):
        label = theme.get("label", f"Theme {i}")
        description = theme.get("description", "")
        severity = theme.get("severity", "")
        icon = {"high": "🔴", "medium": "🟡", "positive": "🟢", "low": "🟢"}.get(severity, "⚪")
        qcount = theme.get("quote_count", "")
        lines.append(f"### {icon} {label}" + (f"  _{qcount} quotes_" if qcount else ""))
        if description:
            lines.append(description)
        lines.append("")

    if findings:
        lines += ["## Key Findings", ""]
        for i, finding in enumerate(findings, 1):
            text = finding.get("finding", finding.get("text", ""))
            sq = finding.get("supporting_quote", {})
            lines.append(f"**{i}. {text}**")
            if sq:
                quote_text = sq.get("text", "")
                speaker = sq.get("speaker", "")
                timestamp = sq.get("timestamp", "")
                if quote_text:
                    lines.append("")
                    lines.append(f'> "{quote_text}"')
                    attr = " — ".join(filter(None, [speaker, timestamp]))
                    if attr:
                        lines.append(f"> *{attr}*")
            lines.append("")

    if quotes:
        lines += ["## Verbatim Quotes", ""]
        groups: dict = {}
        for q in quotes:
            t = q.get("theme", "Other")
            groups.setdefault(t, []).append(q)
        for theme_label, tq in groups.items():
            lines.append(f"### {theme_label}")
            lines.append("")
            for q in tq:
                text = q.get("text", "")
                speaker = q.get("speaker", "")
                timestamp = q.get("timestamp", "")
                lines.append(f'> "{text}"')
                attr = " — ".join(filter(None, [speaker, timestamp]))
                if attr:
                    lines.append(f"> *{attr}*")
                lines.append("")

    lines += ["---", "*Generated by SenseCritiq — sensecritiq.com*"]
    return "\n".join(lines)


def _build_pdf(session_id: str, session_name: str, data: dict):
    """Render synthesis data as a PDF using reportlab. Returns (bytes, ext, content_type)."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
        from reportlab.lib.enums import TA_CENTER
    except ImportError:
        # Reportlab not installed — fall back to markdown
        md = _build_markdown(session_id, session_name, data)
        return md.encode("utf-8"), "md", "text/markdown"

    themes = data.get("themes", [])
    quotes = data.get("quotes", [])
    findings = data.get("findings", [])
    generated_at = datetime.now(timezone.utc).strftime("%B %d, %Y")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=20*mm, bottomMargin=20*mm,
    )
    base = getSampleStyleSheet()
    title_style = ParagraphStyle("T", parent=base["Title"],
        fontSize=20, textColor=colors.HexColor("#1A202C"), spaceAfter=4)
    meta_style = ParagraphStyle("M", parent=base["Normal"],
        fontSize=9, textColor=colors.HexColor("#718096"), spaceAfter=2)
    h2_style = ParagraphStyle("H2", parent=base["Heading2"],
        fontSize=14, textColor=colors.HexColor("#2D3748"), spaceBefore=14, spaceAfter=6)
    h3_style = ParagraphStyle("H3", parent=base["Heading3"],
        fontSize=11, textColor=colors.HexColor("#2D3748"), spaceBefore=8, spaceAfter=4)
    body_style = ParagraphStyle("B", parent=base["Normal"],
        fontSize=10, textColor=colors.HexColor("#4A5568"), leading=15, spaceAfter=6)
    quote_style = ParagraphStyle("Q", parent=base["Normal"],
        fontSize=10, textColor=colors.HexColor("#2D3748"),
        leftIndent=12, leading=15, borderColor=colors.HexColor("#E2E8F0"),
        borderWidth=2, borderPad=8, backColor=colors.HexColor("#F7FAFC"), spaceAfter=4)
    attribution_style = ParagraphStyle("A", parent=base["Normal"],
        fontSize=9, textColor=colors.HexColor("#718096"), leftIndent=12, spaceAfter=10)
    finding_style = ParagraphStyle("F", parent=base["Normal"],
        fontSize=10, textColor=colors.HexColor("#1A202C"), leading=15,
        spaceBefore=6, spaceAfter=4, fontName="Helvetica-Bold")
    footer_style = ParagraphStyle("FT", parent=base["Normal"],
        fontSize=8, textColor=colors.HexColor("#A0AEC0"), alignment=TA_CENTER)

    story = []
    story.append(Paragraph(session_name, title_style))
    story.append(Paragraph(
        f"Generated {generated_at} &nbsp;|&nbsp; {len(quotes)} quotes captured",
        meta_style,
    ))
    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#E2E8F0")))
    story.append(Spacer(1, 4*mm))

    # Themes
    story.append(Paragraph("Themes", h2_style))
    for theme in themes:
        label = theme.get("label", "")
        description = theme.get("description", "")
        severity = theme.get("severity", "")
        cmap = {"high": "#E53E3E", "medium": "#D69E2E", "positive": "#38A169", "low": "#38A169"}
        dot = cmap.get(severity, "#A0AEC0")
        story.append(Paragraph(f'<font color="{dot}">&#9679;</font> <b>{label}</b>', h3_style))
        if description:
            story.append(Paragraph(description, body_style))

    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#E2E8F0")))

    # Key Findings
    if findings:
        story.append(Paragraph("Key Findings", h2_style))
        for i, finding in enumerate(findings, 1):
            text = finding.get("finding", finding.get("text", ""))
            sq = finding.get("supporting_quote", {})
            story.append(Paragraph(f"{i}. {text}", finding_style))
            if sq:
                qtext = sq.get("text", "")
                speaker = sq.get("speaker", "")
                ts = sq.get("timestamp", "")
                if qtext:
                    story.append(Paragraph(f'"{qtext}"', quote_style))
                    attr = " — ".join(filter(None, [speaker, ts]))
                    if attr:
                        story.append(Paragraph(attr, attribution_style))
        story.append(Spacer(1, 4*mm))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#E2E8F0")))

    # Quotes
    if quotes:
        story.append(Paragraph("Verbatim Quotes", h2_style))
        groups: dict = {}
        for q in quotes:
            t = q.get("theme", "Other")
            groups.setdefault(t, []).append(q)
        for theme_label, tq in groups.items():
            story.append(Paragraph(theme_label, h3_style))
            for q in tq:
                text = q.get("text", "")
                speaker = q.get("speaker", "")
                ts = q.get("timestamp", "")
                story.append(Paragraph(f'"{text}"', quote_style))
                attr = " — ".join(filter(None, [speaker, ts]))
                if attr:
                    story.append(Paragraph(attr, attribution_style))

    story.append(Spacer(1, 8*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#E2E8F0")))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph("Generated by SenseCritiq — sensecritiq.com", footer_style))

    doc.build(story)
    buf.seek(0)
    return buf.read(), "pdf", "application/pdf"
