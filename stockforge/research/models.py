"""Data structures and fingerprinting for commercial concepts and research (SF-006)."""
from __future__ import annotations

import hashlib
import re
from typing import List, Optional
from pydantic import BaseModel, Field


class CommercialConceptData(BaseModel):
    """Pydantic model representing a generated distinct commercial concept."""
    title: str = Field(..., max_length=200)
    prompt: str = Field(..., min_length=15)
    commercial_intent: str = Field(default="commercial stock photography")
    technical_specs: str = Field(default="clean lighting, high resolution, 8k, sharp focus")
    sources: List[str] = Field(default_factory=list)
    evidence_quality: str = Field(default="unverified")  # verified, estimate, unverified
    score: Optional[float] = None
    score_method: Optional[str] = None


def normalize_prompt_text(text: str) -> str:
    """Normalize text by lowercasing, removing punctuation, and collapsing whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    tokens = text.split()
    return " ".join(tokens)


def compute_concept_fingerprint(niche_name: str, title: str, prompt: str) -> str:
    """Generate deterministic SHA-256 canonical hash to detect duplicate ideas.
    
    Invariants:
    - Normalizes text variations to prevent superficial deduplication bypass.
    - Scoped to niche and canonical prompt semantics.
    """
    norm_niche = normalize_prompt_text(niche_name)
    norm_title = normalize_prompt_text(title)
    norm_prompt = normalize_prompt_text(prompt)

    canonical_representation = f"niche:{norm_niche}|title:{norm_title}|prompt:{norm_prompt}"
    return hashlib.sha256(canonical_representation.encode("utf-8")).hexdigest()
