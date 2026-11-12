"""Tests for Research, Concept Generation, Provenance, and Fingerprinting (SF-006)."""
from __future__ import annotations

import asyncio
import json
import pytest
from pathlib import Path
from typing import Any, Dict, List, Optional
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from stockforge.db.models import Base, Concept, Niche
from stockforge.providers.base import (
    ProviderCapability,
    ProviderKind,
    ProviderResponse,
    ProviderTextResponse,
    RoleType,
    TextProvider,
)
from stockforge.providers.mock import MockTextProvider
from stockforge.providers.registry import ProviderRegistry
from stockforge.providers.router import ProviderRouter
from stockforge.research.models import (
    CommercialConceptData,
    compute_concept_fingerprint,
    normalize_prompt_text,
)
from stockforge.research.service import (
    ConceptApprovalError,
    DuplicateConceptError,
    ResearchResult,
    ResearchService,
)


@pytest.fixture
def test_db(tmp_path: Path):
    """Create an isolated temporary SQLite database for testing."""
    db_path = tmp_path / "test_research.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return session_factory


class TrackingTextProvider:
    """Mock text provider that tracks calls and returns customizable mock responses."""

    def __init__(self, response_text: str = "") -> None:
        self.response_text = response_text
        self.call_count = 0
        self.recorded_prompts: List[str] = []

    async def generate_text(
        self,
        prompt: str,
        model: str,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        operation_id: Optional[str] = None,
        **kwargs: Any,
    ) -> ProviderTextResponse:
        self.call_count += 1
        self.recorded_prompts.append(prompt)
        return ProviderTextResponse(
            request_id=f"req-mock-{self.call_count}",
            provider="mock_text",
            model=model,
            role=RoleType.PROMPT,
            text=self.response_text,
            prompt_tokens=100,
            completion_tokens=200,
        )


def _generate_mock_concepts_json(count: int, duplicate: bool = False) -> str:
    """Generate mock JSON response containing valid commercial stock concepts."""
    items = []
    for i in range(1, count + 1):
        # If duplicate flag is True, duplicate the first concept for the second concept
        idx = 1 if (duplicate and i == 2) else i
        items.append({
            "title": f"Sustainable Green Architecture Office Concept {idx}",
            "prompt": f"Modern eco-friendly office interior with vertical garden wall and solar glass windows #{idx}",
            "commercial_intent": "Corporate sustainability marketing and stock library",
            "technical_specs": "Bright natural diffused lighting, clean composition, 8k resolution, photorealistic",
            "sources": ["Adobe Stock Q3 Sustainability Report"] if idx % 2 == 1 else [],
            "evidence_quality": "verified" if idx % 2 == 1 else "unverified",
            "score": 0.85,
            "score_method": "Heuristic commercial viability score",
        })
    return json.dumps(items)


def test_concept_fingerprinting_canonical():
    """Verify that fingerprinting normalizes whitespace, punctuation, and capitalization."""
    fp1 = compute_concept_fingerprint(
        "Sustainable Architecture",
        "Modern Eco-friendly Office",
        "Sunlit interior with vertical garden, clean 8k focus.",
    )
    fp2 = compute_concept_fingerprint(
        "sustainable architecture",
        "Modern Eco-Friendly Office!",
        "sunlit   interior with vertical garden clean 8k focus",
    )
    assert fp1 == fp2, "Fingerprint canonicalization harus menghasilkan hash identik untuk teks semantik serupa"

    # Distinct prompt produces distinct fingerprint
    fp3 = compute_concept_fingerprint(
        "Sustainable Architecture",
        "Modern Eco-friendly Office",
        "Night view with illuminated led garden, high tech focus.",
    )
    assert fp1 != fp3


def test_generate_10_validated_distinct_concepts_persisted(test_db):
    """Verify acceptance criterion: 10 distinct concepts generated and persisted in 'draft' status."""
    mock_json = _generate_mock_concepts_json(10, duplicate=False)
    mock_provider = TrackingTextProvider(response_text=mock_json)

    service = ResearchService(session_factory=test_db, text_provider=mock_provider)

    result = asyncio.run(service.generate_concepts(niche_identifier="Eco Architecture", count=10))

    assert result.total_generated == 10
    assert result.total_duplicates == 0
    assert len(result.concepts) == 10

    # Verify database persistence
    with test_db() as session:
        db_concepts = session.scalars(select(Concept).where(Concept.niche_id == result.niche.id)).all()
        assert len(db_concepts) == 10
        for c in db_concepts:
            assert c.status == "draft", "Semua konsep yang baru di-generate wajib berstatus 'draft' (Human Approval Invariant)"
            assert len(c.fingerprint) == 64
            # Verify parsed sources payload
            meta = json.loads(c.research_sources_json)
            assert "evidence_quality" in meta


def test_duplicate_concept_flagged_and_rejected(test_db):
    """Verify acceptance criterion: duplicate ideas are flagged and prevented from duplicate persistence."""
    # Item 2 is an exact duplicate of Item 1
    mock_json = _generate_mock_concepts_json(5, duplicate=True)
    mock_provider = TrackingTextProvider(response_text=mock_json)

    service = ResearchService(session_factory=test_db, text_provider=mock_provider)

    result = asyncio.run(service.generate_concepts(niche_identifier="Eco Architecture", count=5))

    assert result.total_generated == 4, "Konsep valid yang tersimpan harus 4 karena 1 duplikat"
    assert result.total_duplicates == 1, "Tepat 1 konsep harus ditandai sebagai duplikat"
    assert "Duplicate concept fingerprint detected" in result.flagged_duplicates[0]["reason"]

    # Re-running the exact same batch should flag all as duplicates
    result_run2 = asyncio.run(service.generate_concepts(niche_identifier="Eco Architecture", count=5))
    assert result_run2.total_generated == 0, "Semua konsep di run kedua seharusnya dideteksi sebagai duplikat database"
    assert result_run2.total_duplicates == 5


def test_provenance_sanitization_unverified_sources(test_db):
    """Verify that concepts lacking sources are strictly labeled as unverified evidence."""
    mock_json = json.dumps([
        {
            "title": "Unverified High Yield Claim Concept",
            "prompt": "Stock photo prompt claiming 9999% profit and trending guarantee",
            "commercial_intent": "Stock asset",
            "sources": [],  # Empty sources
            "evidence_quality": "verified",  # Misleading claim by provider
            "score": 0.99,
            "score_method": "verified partner official data",
        }
    ])
    mock_provider = TrackingTextProvider(response_text=mock_json)
    service = ResearchService(session_factory=test_db, text_provider=mock_provider)

    result = asyncio.run(service.generate_concepts(niche_identifier="Financial Trends", count=1))

    assert result.total_generated == 1
    assert result.unverified_count == 1
    concept = result.concepts[0]

    meta = json.loads(concept.research_sources_json)
    assert meta["evidence_quality"] == "unverified", "Klaim tanpa sumber wajib disanitasi menjadi 'unverified'"
    assert meta["score_method"] != "verified partner official data"


def test_zero_image_provider_calls_invariant(test_db):
    """Verify hard invariant: zero calls to image providers during research and concept generation."""
    registry = ProviderRegistry()
    registry.register_provider("mock_text", ProviderKind.TEXT, "https://mock.text", "TEXT_KEY")
    registry.register_provider("mock_img", ProviderKind.IMAGE, "https://mock.image", "IMG_KEY")

    registry.register_model(
        model_id="mock-text-model",
        provider_name="mock_text",
        kind=ProviderKind.TEXT,
        capabilities={ProviderCapability.TEXT_GENERATION},
    )
    registry.register_model(
        model_id="mock-img-model",
        provider_name="mock_img",
        kind=ProviderKind.IMAGE,
        capabilities={ProviderCapability.IMAGE_GENERATION, ProviderCapability.COMMERCIAL_LICENSE_VERIFIED},
    )

    router = ProviderRouter(registry)
    router.set_route(RoleType.RESEARCH, "mock-text-model")
    router.set_route(RoleType.PROMPT, "mock-text-model")
    router.set_route(RoleType.IMAGE, "mock-img-model")

    mock_text = TrackingTextProvider(response_text=_generate_mock_concepts_json(2))
    registry._provider_instances = {"mock_text": mock_text}

    service = ResearchService(session_factory=test_db, router=router)

    # 1. Test discover_niches
    mock_text.response_text = json.dumps([
        {"name": "Bio-Tech Lab", "priority": 1, "source_notes": "Trends", "rationale": "High demand"}
    ])
    niches = asyncio.run(service.discover_niches())
    assert len(niches) == 1
    assert mock_text.call_count == 1

    # 2. Test generate_concepts
    mock_text.response_text = _generate_mock_concepts_json(2)
    result = asyncio.run(service.generate_concepts(niche_identifier="Bio-Tech Lab", count=2))
    assert result.total_generated == 2

    # Invariant: ImageProvider was NEVER invoked or queried
    with pytest.raises(ValueError, match="Invariant violation: ImageProvider tidak boleh dipanggil"):
        asyncio.run(service._get_text_provider(RoleType.IMAGE))


def test_human_approval_state_machine(test_db):
    """Verify human concept approval and rejection state transitions."""
    mock_json = _generate_mock_concepts_json(2)
    mock_provider = TrackingTextProvider(response_text=mock_json)
    service = ResearchService(session_factory=test_db, text_provider=mock_provider)

    result = asyncio.run(service.generate_concepts(niche_identifier="Urban Farming", count=2))
    concept1 = result.concepts[0]
    concept2 = result.concepts[1]

    # Initial state is draft
    assert concept1.status == "draft"
    assert concept2.status == "draft"

    # Approve concept1
    approved = service.approve_concept(concept1.id, user_id=12345)
    assert approved.status == "approved"

    # Reject concept2
    rejected = service.reject_concept(concept2.id, reason="Low commercial demand", user_id=12345)
    assert rejected.status == "rejected"

    # Attempting to approve an already approved concept must raise ConceptApprovalError
    with pytest.raises(ConceptApprovalError):
        service.approve_concept(concept1.id)

    # Attempting to reject an already rejected concept must raise ConceptApprovalError
    with pytest.raises(ConceptApprovalError):
        service.reject_concept(concept2.id)

    # Attempting to approve an already rejected concept must raise ConceptApprovalError
    with pytest.raises(ConceptApprovalError):
        service.approve_concept(concept2.id)


def test_list_concepts_filtering(test_db):
    """Verify listing and filtering concepts by niche and status."""
    mock_json = _generate_mock_concepts_json(3)
    mock_provider = TrackingTextProvider(response_text=mock_json)
    service = ResearchService(session_factory=test_db, text_provider=mock_provider)

    result = asyncio.run(service.generate_concepts(niche_identifier="Culinary Art", count=3))
    c1, c2, c3 = result.concepts

    service.approve_concept(c1.id)
    service.reject_concept(c2.id)

    all_concepts = service.list_concepts(niche_id=result.niche.id)
    assert len(all_concepts) == 3

    draft_concepts = service.list_concepts(status="draft")
    assert len(draft_concepts) == 1
    assert draft_concepts[0].id == c3.id

    approved_concepts = service.list_concepts(status="approved")
    assert len(approved_concepts) == 1
    assert approved_concepts[0].id == c1.id
