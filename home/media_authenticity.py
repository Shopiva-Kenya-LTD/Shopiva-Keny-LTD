"""Shopiva media authenticity screening.

This is a moderation aid, not forensic proof. It never treats a probabilistic AI result as
certain. With OPENAI_API_KEY configured, a multimodal model reviews images or a representative
video frame; without it, media is safely marked for human review.
"""
import base64
import json
import os
import subprocess
import tempfile


def _fallback(reason):
    return {"status": "needs_review", "score": 50, "notes": reason}


def _parse_review(text):
    try:
        data = json.loads(text)
        status = str(data.get("status", "needs_review")).strip().lower()
        if status not in {"likely_real", "needs_review", "likely_ai"}:
            status = "needs_review"
        score = max(0, min(100, int(data.get("ai_score", 50))))
        notes = str(data.get("notes", "")).strip()[:500]
        return {"status": status, "score": score, "notes": notes or "Media requires human review."}
    except Exception:
        return _fallback("AI screening returned an unreadable result; human review required.")


def _review_image_bytes(raw_bytes, mime_type, label):
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return _fallback("Automated AI screening is not configured; human review required.")
    try:
        from openai import OpenAI
        encoded = base64.b64encode(raw_bytes).decode("ascii")
        client = OpenAI(api_key=api_key)
        response = client.responses.create(
            model=os.getenv("OPENAI_MEDIA_REVIEW_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.6-luna")),
            instructions=(
                "You are Shopiva's product-media authenticity screening assistant. "
                "Review seller media conservatively. Do not identify people. AI-generation detection "
                "is probabilistic. Return ONLY JSON with keys status, ai_score, notes. status must be "
                "likely_real, needs_review, or likely_ai. ai_score is 0-100 and represents estimated "
                "likelihood of generative AI. Use needs_review whenever evidence is ambiguous. Do not "
                "reject a seller solely from this result. Mention concrete visual inconsistencies briefly."
            ),
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": f"Review this Shopiva {label} for possible generative-AI creation."},
                    {"type": "input_image", "image_url": f"data:{mime_type};base64,{encoded}"},
                ],
            }],
        )
        return _parse_review(response.output_text)
    except Exception:
        return _fallback("Automated screening was unavailable; human review required.")


def screen_image(uploaded_file):
    try:
        uploaded_file.seek(0)
        raw = uploaded_file.read()
        mime = getattr(uploaded_file, "content_type", None) or "image/jpeg"
        result = _review_image_bytes(raw, mime, "product image")
        uploaded_file.seek(0)
        return result
    except Exception:
        try:
            uploaded_file.seek(0)
        except Exception:
            pass
        return _fallback("The image could not be screened automatically.")


def screen_video(uploaded_file):
    """Screen a representative first frame when ffmpeg is available."""
    src_path = None
    try:
        uploaded_file.seek(0)
        suffix = ".mp4"
        name = str(getattr(uploaded_file, "name", "")).lower()
        for ext in (".mp4", ".mov", ".webm", ".m4v"):
            if name.endswith(ext):
                suffix = ext
                break
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as src:
            src.write(uploaded_file.read())
            src_path = src.name
        frame = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "0", "-i", src_path,
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            capture_output=True,
            timeout=20,
            check=True,
        ).stdout
        if not frame:
            return _fallback("No representative video frame was available for screening.")
        return _review_image_bytes(frame, "image/jpeg", "product video frame")
    except Exception:
        return _fallback("Video frame screening is unavailable; human review required.")
    finally:
        if src_path:
            try:
                os.unlink(src_path)
            except OSError:
                pass
        try:
            uploaded_file.seek(0)
        except Exception:
            pass
