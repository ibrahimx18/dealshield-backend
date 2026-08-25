"""AI integration router for Ollama LLM calls.
- /ai/chat: general chat
- /ai/fraud-check: analyze a listing for potential fraud
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import requests
import os
import json

router = APIRouter()

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str


class FraudCheckRequest(BaseModel):
    title: str
    description: str
    price: float
    category: str
    seller_name: str = ""
    seller_rating: float = 5.0
    seller_deals: int = 0


class FraudCheckResponse(BaseModel):
    risk_score: int  # 0-100
    risk_level: str  # low, medium, high
    flags: list
    recommendation: str


FRAUD_PROMPT = """You are a fraud detection AI for SafePay, a Nigerian escrow platform.
Analyze this listing for potential fraud risk.

Listing:
- Title: {title}
- Description: {description}
- Price: ₦{price:,.0f}
- Category: {category}
- Seller: {seller_name} (Rating: {seller_rating}, Deals: {seller_deals})

Respond ONLY with valid JSON (no markdown, no explanation):
{{
  "risk_score": <0-100 integer>,
  "risk_level": "<low|medium|high>",
  "flags": ["<list of specific red flags, empty if none>"],
  "recommendation": "<one sentence advice to buyer>"
}}

Red flags to check:
- Price too good to be true (significantly below market)
- Vague or generic description
- Seller has no rating or very few deals
- Pressure tactics in description ("urgent", "last one", "must sell today")
- No location or vague location
- Category mismatch (e.g. car priced like phone)
- Unusual payment requests
- Too good to be true for gold/dollars (common scam categories)
"""


@router.post("/chat", response_model=ChatResponse, summary="Chat with local LLM via Ollama")
async def chat(req: ChatRequest):
    """Send a user message to Ollama and return the assistant reply."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": req.message}],
        "stream": False,
    }
    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to contact Ollama: {e}")
    data = resp.json()
    return ChatResponse(reply=data.get("message", {}).get("content", ""))


@router.post("/fraud-check", response_model=FraudCheckResponse, summary="AI fraud analysis of a listing")
async def fraud_check(req: FraudCheckRequest):
    """Analyze a listing for fraud risk using Ollama."""
    prompt = FRAUD_PROMPT.format(
        title=req.title,
        description=req.description,
        price=req.price,
        category=req.category,
        seller_name=req.seller_name or "Unknown",
        seller_rating=req.seller_rating,
        seller_deals=req.seller_deals,
    )
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
    }
    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Fraud check failed: {e}")

    data = resp.json()
    content = data.get("message", {}).get("content", "{}")

    try:
        result = json.loads(content)
        # Ensure required fields
        return FraudCheckResponse(
            risk_score=min(100, max(0, int(result.get("risk_score", 0)))),
            risk_level=result.get("risk_level", "low"),
            flags=result.get("flags", []),
            recommendation=result.get("recommendation", "No recommendation"),
        )
    except (json.JSONDecodeError, ValueError):
        # Fallback if LLM doesn't return valid JSON
        return FraudCheckResponse(
            risk_score=50,
            risk_level="medium",
            flags=["AI analysis unavailable — manual review recommended"],
            recommendation="Could not analyze automatically. Proceed with caution.",
        )
