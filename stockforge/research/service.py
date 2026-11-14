"""Research and concept generation service (SF-006)."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Union
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockforge.db.models import Concept, Niche
from stockforge.providers.base import ProviderKind, RoleType, TextProvider
from stockforge.providers.router import ProviderRouter
from stockforge.research.models import (
    CommercialConceptData,
    compute_concept_fingerprint,
    normalize_prompt_text,
)

logger = logging.getLogger("stockforge.research.service")


class ConceptApprovalError(Exception):
    """Raised when an invalid state transition occurs during concept review."""
    pass


class DuplicateConceptError(Exception):
    """Raised when an exact or duplicate concept fingerprint is detected."""
    pass


class ResearchResult:
    """Encapsulates the outcome of a concept generation run."""

    def __init__(
        self,
        niche: Optional[Niche],
        concepts: List[Concept],
        flagged_duplicates: List[Dict[str, Any]],
        unverified_count: int,
    ) -> None:
        self.niche = niche
        self.concepts = concepts
        self.flagged_duplicates = flagged_duplicates
        self.unverified_count = unverified_count

    @property
    def total_generated(self) -> int:
        return len(self.concepts)

    @property
    def total_duplicates(self) -> int:
        return len(self.flagged_duplicates)


class ResearchService:
    """Service governing niche discovery, commercial prompt research, and provenance validation.
    
    Hard Invariants:
    1. Zero Image Calls: Never invokes any ImageProvider during research or concept generation.
    2. Human Approval Invariant: Concepts are saved in 'draft' status; require explicit human approval.
    3. Strict Provenance: Claims without verifiable external sources are marked 'unverified'.
    4. Canonical Fingerprinting: Prevents duplicate concepts within and across niches.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        router: Optional[ProviderRouter] = None,
        text_provider: Optional[TextProvider] = None,
    ) -> None:
        self.session_factory = session_factory
        self.router = router
        self.text_provider = text_provider

    async def _get_text_provider(self, role: RoleType) -> tuple[TextProvider, str]:
        """Resolve a text provider and model for the specified role.
        
        Enforces invariant: Role MUST require TEXT_GENERATION and never IMAGE_GENERATION.
        """
        if role == RoleType.IMAGE:
            raise ValueError("Invariant violation: ImageProvider tidak boleh dipanggil selama proses research.")

        if self.text_provider is not None:
            return self.text_provider, "mock-model"

        if self.router is None:
            raise RuntimeError("ProviderRouter atau TextProvider harus disediakan untuk ResearchService.")

        model_id = self.router.resolve_model(role, fallback_attempt=0)
        model_reg = self.router.registry.get_model(model_id)
        if not model_reg:
            raise KeyError(f"Model '{model_id}' tidak ditemukan dalam registry.")

        provider_instance = getattr(self.router.registry, "_provider_instances", {}).get(model_reg.provider_name)
        if provider_instance is None:
            from stockforge.providers.mock import MockTextProvider
            provider_instance = MockTextProvider(provider_name=model_reg.provider_name)

        return provider_instance, model_id

    def sanitize_provenance(self, concept_data: CommercialConceptData) -> CommercialConceptData:
        """Validate and sanitize source claims to prevent fabricated market metrics."""
        cleaned_sources = [s.strip() for s in concept_data.sources if s.strip()]
        
        # If no valid sources are provided, evidence MUST be labeled as unverified
        if not cleaned_sources:
            concept_data.sources = []
            concept_data.evidence_quality = "unverified"
            # Prevent misleading authoritative score if unsupported
            if concept_data.score_method and "verified" in concept_data.score_method.lower():
                concept_data.score_method = "unverified_estimate"
        else:
            concept_data.sources = cleaned_sources
            if concept_data.evidence_quality not in ("verified", "estimate", "unverified"):
                concept_data.evidence_quality = "unverified"

        return concept_data

    def _parse_concepts_json(self, raw_text: str) -> List[CommercialConceptData]:
        """Extract and parse JSON array of concept proposals from LLM output."""
        # Find JSON array block if wrapped in markdown code fence
        match = re.search(r"\[.*\]", raw_text, re.DOTALL)
        if match:
            json_str = match.group(0)
        else:
            json_str = raw_text.strip()

        try:
            data = json.loads(json_str)
            if not isinstance(data, list):
                raise ValueError("Output LLM bukan berupa JSON array.")
        except Exception as e:
            logger.warning(f"Gagal melakukan parse JSON konsep langsung: {e}. Menggunakan parsing fallback...")
            # Fallback line-by-line simple parse or re-raise
            raise ValueError(f"Format respon konsep tidak valid: {e}")

        concepts = []
        for item in data:
            if isinstance(item, dict):
                try:
                    concept = CommercialConceptData(**item)
                    concepts.append(self.sanitize_provenance(concept))
                except Exception as ex:
                    logger.debug(f"Mengabaikan item konsep tidak valid: {ex}")
        return concepts

    async def discover_niches(
        self,
        domain_query: str = "commercial microstock photography trends",
        count: int = 5,
    ) -> List[Dict[str, Any]]:
        """Discover candidate niches using text provider without triggering image generation."""
        provider, model = await self._get_text_provider(RoleType.RESEARCH)

        system_prompt = (
            f"You are a commercial stock photography research analyst. "
            f"Analyze current commercial demand and suggest {count} distinct stock photo niches for: '{domain_query}'. "
            f"Keep each rationale concise (1-2 sentences) and source_notes brief so the response is complete. "
            f"Return ONLY a valid JSON array of objects with keys: "
            f"['name', 'priority', 'source_notes', 'rationale']. "
            f"Do NOT invent false statistics or claim unverified partner data. "
            f"Do NOT wrap with explanation or markdown formatting."
        )

        resp = await provider.generate_text(
            prompt=system_prompt,
            model=model,
            max_tokens=4000,
            temperature=0.7,
        )

        raw = resp.text.strip()
        if "```" in raw:
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
            raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)

        match = re.search(r"\[.*\]", raw, re.DOTALL)
        json_str = match.group(0) if match else raw.strip()
        try:
            niches_data = json.loads(json_str)
            if isinstance(niches_data, list):
                return niches_data[:count]
        except Exception as e:
            # Attempt repair if closing bracket was truncated at the end
            if json_str.rfind("}") > 0 and not json_str.endswith("]"):
                try:
                    repaired = json_str[:json_str.rfind("}") + 1] + "\n]"
                    niches_data = json.loads(repaired)
                    if isinstance(niches_data, list):
                        return niches_data[:count]
                except Exception:
                    pass
            logger.error(f"Gagal mem-parse respon discovery niche: {e} (Raw preview: {raw[:200]})")
            raise ValueError(f"Respon niche dari provider tidak valid: {e}")

        return []

    async def generate_concepts(
        self,
        niche_identifier: Union[int, str],
        count: int = 10,
        commercial_intent: str = "commercial microstock photography for Adobe Stock",
    ) -> ResearchResult:
        """Generate distinct, validated commercial concepts for a niche and persist in 'draft' status.
        
        Acceptance:
        - Exactly or at least `count` distinct concepts generated.
        - Fingerprint computed via canonical normalization.
        - Duplicates detected, recorded, and prevented from persisting twice.
        - Strictly zero image provider calls.
        - Human approval invariant: saved as status 'draft'.
        """
        with self.session_factory() as session:
            # 1. Resolve or create Niche
            if isinstance(niche_identifier, int):
                niche = session.get(Niche, niche_identifier)
                if not niche:
                    raise ValueError(f"Niche dengan ID {niche_identifier} tidak ditemukan.")
            else:
                niche_name = niche_identifier.strip()
                stmt = select(Niche).where(Niche.name == niche_name)
                niche = session.scalar(stmt)
                if not niche:
                    niche = Niche(name=niche_name, priority=1, source_notes="Auto-created during research")
                    session.add(niche)
                    session.commit()
                    session.refresh(niche)

            niche_id = niche.id
            niche_name = niche.name

            # 2. Load existing fingerprints in DB to prevent duplicates across runs
            existing_fps = set(
                session.scalars(select(Concept.fingerprint).where(Concept.niche_id == niche_id)).all()
            )

        # 3. Call TextProvider (RoleType.PROMPT or RoleType.RESEARCH)
        provider, model = await self._get_text_provider(RoleType.PROMPT)

        prompt_instruction = (
            f"Generate {count} unique, high-commercial-value stock photography concepts for niche: '{niche_name}'. "
            f"Commercial intent: '{commercial_intent}'. "
            f"Requirements:\n"
            f"- Output strictly valid JSON array of {count} objects.\n"
            f"- ANTI-PLAGIARISM & IP SAFETY RULES (MANDATORY):\n"
            f"  - Concepts and prompts MUST be 100% original and strictly free from plagiarism.\n"
            f"  - FORBIDDEN: Any trademarked brand names (e.g. Apple, Nike, Starbucks), corporate logos, real celebrity likenesses, copyrighted characters (e.g. Disney, Marvel), or artist mimicry ('in the style of [Artist]').\n"
            f"  - All depicted products, devices, vehicles, apparel, and architecture MUST be generic, unbranded, and commercially safe.\n"
            f"- DEDUPLICATION RULES (MANDATORY):\n"
            f"  - Every concept MUST be distinct in subject, composition, camera angle, and environment.\n"
            f"  - Do NOT generate near-duplicate concepts with minor color or word variations.\n"
            f"- Each object MUST contain:\n"
            f"  - 'title': concise commercial stock title (max 200 chars)\n"
            f"  - 'prompt': detailed production prompt with subject, setting, lighting, composition (min 15 chars)\n"
            f"  - 'commercial_intent': intended stock buyer use case\n"
            f"  - 'technical_specs': camera angle, lighting, 8k, sharp focus\n"
            f"  - 'sources': array of verified market data sources or empty array if unverified\n"
            f"  - 'evidence_quality': 'verified', 'estimate', or 'unverified'\n"
            f"  - 'score': optional float score (0.0 to 1.0)\n"
            f"  - 'score_method': brief description of scoring methodology\n"
            f"- Do NOT fabricate false partner statistics or invent proprietary platform revenue figures."
        )

        resp = await provider.generate_text(
            prompt=prompt_instruction,
            model=model,
            max_tokens=3000,
            temperature=0.7,
        )

        parsed_concepts = self._parse_concepts_json(resp.text)

        persisted_concepts: List[Concept] = []
        flagged_duplicates: List[Dict[str, Any]] = []
        unverified_count: int = 0
        seen_in_batch_fps: set[str] = set()

        with self.session_factory() as session:
            db_niche = session.get(Niche, niche_id)

            for item in parsed_concepts:
                fp = compute_concept_fingerprint(niche_name, item.title, item.prompt)

                if item.evidence_quality == "unverified":
                    unverified_count += 1

                # Check duplicate against DB or current batch
                if fp in existing_fps or fp in seen_in_batch_fps:
                    logger.warning(f"Konsep duplikat terdeteksi (fingerprint={fp[:12]}): '{item.title}'")
                    flagged_duplicates.append({
                        "title": item.title,
                        "prompt": item.prompt,
                        "fingerprint": fp,
                        "reason": "Duplicate concept fingerprint detected.",
                    })
                    continue

                seen_in_batch_fps.add(fp)
                existing_fps.add(fp)

                sources_payload = {
                    "sources": item.sources,
                    "evidence_quality": item.evidence_quality,
                    "commercial_intent": item.commercial_intent,
                    "technical_specs": item.technical_specs,
                    "score": item.score,
                    "score_method": item.score_method,
                }

                concept_entity = Concept(
                    niche_id=niche_id,
                    title=item.title,
                    prompt=item.prompt,
                    fingerprint=fp,
                    research_sources_json=json.dumps(sources_payload),
                    status="draft",  # Strict Invariant: concepts start in 'draft'
                )
                session.add(concept_entity)
                persisted_concepts.append(concept_entity)

            session.commit()
            for c in persisted_concepts:
                session.refresh(c)

        return ResearchResult(
            niche=db_niche,
            concepts=persisted_concepts,
            flagged_duplicates=flagged_duplicates,
            unverified_count=unverified_count,
        )

    def approve_concept(self, concept_id: int, user_id: Optional[int] = None) -> Concept:
        """Transition a concept from 'draft' to 'approved'.
        
        Strict Human Approval Invariant:
        Only concepts in 'draft' state can be approved.
        """
        with self.session_factory() as session:
            concept = session.get(Concept, concept_id)
            if not concept:
                raise ValueError(f"Concept dengan ID {concept_id} tidak ditemukan.")

            if concept.status != "draft":
                raise ConceptApprovalError(
                    f"Transisi tidak valid: Konsep #{concept_id} saat ini berstatus '{concept.status}', "
                    f"hanya konsep 'draft' yang dapat disetujui."
                )

            concept.status = "approved"
            session.commit()
            session.refresh(concept)
            logger.info(f"Konsep #{concept_id} disetujui (user_id={user_id}).")
            return concept

    def reject_concept(self, concept_id: int, reason: Optional[str] = None, user_id: Optional[int] = None) -> Concept:
        """Transition a concept from 'draft' to 'rejected'."""
        with self.session_factory() as session:
            concept = session.get(Concept, concept_id)
            if not concept:
                raise ValueError(f"Concept dengan ID {concept_id} tidak ditemukan.")

            if concept.status != "draft":
                raise ConceptApprovalError(
                    f"Transisi tidak valid: Konsep #{concept_id} saat ini berstatus '{concept.status}', "
                    f"hanya konsep 'draft' yang dapat ditolak."
                )

            concept.status = "rejected"
            session.commit()
            session.refresh(concept)
            logger.info(f"Konsep #{concept_id} ditolak (alasan='{reason}', user_id={user_id}).")
            return concept

    def list_concepts(self, niche_id: Optional[int] = None, status: Optional[str] = None) -> List[Concept]:
        """Query concepts filtered by niche and/or review status."""
        with self.session_factory() as session:
            stmt = select(Concept)
            if niche_id is not None:
                stmt = stmt.where(Concept.niche_id == niche_id)
            if status is not None:
                stmt = stmt.where(Concept.status == status)
            stmt = stmt.order_by(Concept.id.asc())
            return list(session.scalars(stmt).all())
