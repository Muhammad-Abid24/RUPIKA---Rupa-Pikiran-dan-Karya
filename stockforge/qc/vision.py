"""Vision quality control and visual metadata generation service (SF-010)."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockforge.db.models import Asset, AssetMetadata, QcResult, utc_now
from stockforge.providers.base import ProviderVisionResponse, RoleType, VisionProvider
from stockforge.providers.router import ProviderRouter
from stockforge.storage.service import StorageService

logger = logging.getLogger("stockforge.qc.vision")

VISION_QC_DISCLAIMER = (
    "Automated vision inspection for technical defect detection and metadata derivation only. "
    "Does not constitute legal certification, copyright clearance, trademark clearance, "
    "or model/property release verification. Human review remains mandatory prior to agency submission."
)

# Standard official 21 Adobe Stock contributor categories
ADOBE_CATEGORIES: Dict[int, str] = {
    1: "Animals",
    2: "Buildings and Architecture",
    3: "Business",
    4: "Drinks",
    5: "The Environment",
    6: "States of Mind",
    7: "Food",
    8: "Graphic Resources",
    9: "Hobbies and Leisure",
    10: "Industry",
    11: "Landscapes",
    12: "Lifestyle",
    13: "People",
    14: "Plants and Flowers",
    15: "Culture and Religion",
    16: "Science",
    17: "Social Issues",
    18: "Sports",
    19: "Technology",
    20: "Transport",
    21: "Travel",
}

# Reverse lookup for category names
ADOBE_CATEGORY_NAMES: Dict[str, int] = {
    name.lower(): cat_id for cat_id, name in ADOBE_CATEGORIES.items()
}

# Anti-hallucination and anti-plagiarism forbidden terms in metadata
FORBIDDEN_BRAND_KEYWORDS = {
    # Tech & Electronics
    "apple", "iphone", "ipad", "macbook", "imac", "airpods", "ios", "samsung", "galaxy",
    "sony", "playstation", "xbox", "nintendo", "switch", "microsoft", "windows", "intel",
    "amd", "nvidia", "google", "pixel", "android", "huawei", "xiaomi", "lenovo", "dell",
    "hp", "canon", "nikon", "panasonic", "fujifilm", "leica",
    # Apparel & Fashion
    "nike", "jordan", "adidas", "puma", "reebok", "under armour", "gucci", "prada",
    "louis vuitton", "chanel", "hermes", "balenciaga", "versace", "armani", "dior",
    "burberry", "rolex", "omega", "cartier", "zara", "hm", "uniqlo", "ray-ban", "rayban",
    # Automotive
    "tesla", "bmw", "mercedes", "audi", "porsche", "ferrari", "lamborghini", "toyota",
    "honda", "ford", "chevrolet", "volkswagen", "nissan", "hyundai", "kia", "volvo",
    # Food, Beverage & Retail
    "coca-cola", "cocacola", "coke", "pepsi", "starbucks", "mcdonalds", "mcdonald's",
    "burger king", "kfc", "subway", "nestle", "red bull",
    # Social & Media Platforms
    "instagram", "facebook", "meta", "tiktok", "twitter", "youtube", "netflix", "spotify", "amazon",
    # Copyrighted Characters / Franchises
    "disney", "marvel", "pixar", "star wars", "pokemon", "pikachu", "mario", "batman",
    "superman", "spiderman", "spider-man", "mickey mouse", "barbie", "harry potter",
}

FORBIDDEN_AI_KEYWORDS = {
    "midjourney", "dall-e", "dalle", "stable diffusion", "stablediffusion",
    "flux", "ai generated", "ai-generated", "photoshop", "generative ai",
}


def clean_title(raw_title: Optional[str], max_len: int = 70) -> str:
    """Sanitize and strictly enforce title length <= max_len without cutting words mid-word."""
    if not raw_title:
        return "Commercial Stock Photo Asset"

    # Remove extra whitespace, newlines, and quotes
    cleaned = re.sub(r"[\r\n\t]+", " ", str(raw_title)).strip()
    cleaned = cleaned.strip("\"' ")

    if not cleaned:
        return "Commercial Stock Photo Asset"

    if len(cleaned) <= max_len:
        return cleaned

    # Truncate at last word boundary within max_len
    truncated = cleaned[:max_len].rsplit(" ", 1)[0].strip()
    # Strip any trailing punctuation that looks awkward
    truncated = re.sub(r"[,;:\-–—\s]+$", "", truncated)

    if not truncated:
        # Single long word or failure to split by space
        truncated = cleaned[:max_len].strip()

    return truncated


def clean_keywords(
    raw_keywords: Union[List[str], str, None],
    max_count: int = 40,
) -> List[str]:
    """Clean, filter anti-hallucination/brand terms, deduplicate and cap keywords at max_count.
    
    Guarantees:
    - Conservative MVP limit <= 40 items.
    - Relevant ordering preserved.
    - Case-insensitive deduplication.
    - Scrubbing of trademarked brand names and AI tool names.
    """
    if not raw_keywords:
        return []

    if isinstance(raw_keywords, str):
        items = [k.strip() for k in raw_keywords.split(",") if k.strip()]
    elif isinstance(raw_keywords, (list, tuple)):
        items = [str(k).strip() for k in raw_keywords if str(k).strip()]
    else:
        return []

    cleaned: List[str] = []
    seen: set[str] = set()

    for item in items:
        # Strip special punctuation except hyphens
        token = re.sub(r"[^\w\s-]", "", item).strip().lower()
        # Collapse multiple spaces
        token = re.sub(r"\s+", " ", token)

        if not token or len(token) < 2:
            continue
        if token in seen:
            continue

        # Anti-hallucination & brand/AI filter
        if token in FORBIDDEN_BRAND_KEYWORDS or token in FORBIDDEN_AI_KEYWORDS:
            logger.warning("Menghapus keyword terlarang/merek: '%s'", token)
            continue

        seen.add(token)
        cleaned.append(token)

        if len(cleaned) >= max_count:
            break

    return cleaned


def validate_category_id(val: Any) -> Optional[int]:
    """Validate and normalize Adobe Stock category ID (1-21)."""
    if val is None:
        return None

    if isinstance(val, int) and 1 <= val <= 21:
        return val

    if isinstance(val, str):
        val_str = val.strip().lower()
        if val_str.isdigit():
            c_int = int(val_str)
            if 1 <= c_int <= 21:
                return c_int
        if val_str in ADOBE_CATEGORY_NAMES:
            return ADOBE_CATEGORY_NAMES[val_str]

    return None


def safe_extract_json(content: Any) -> Dict[str, Any]:
    """Safely extract dictionary from structured response, raw string, or markdown fence."""
    if isinstance(content, dict):
        return content

    if not isinstance(content, str):
        raise ValueError(f"Payload harus berupa dict atau string JSON, ditemukan: {type(content)}")

    raw = content.strip()

    # 1. Direct JSON parse
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # 2. Markdown json code block
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if match:
        try:
            parsed = json.loads(match.group(1).strip())
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    # 3. Outer brace search
    match_braces = re.search(r"\{[\s\S]*\}", raw)
    if match_braces:
        try:
            parsed = json.loads(match_braces.group(0).strip())
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    raise ValueError(f"Tidak dapat mengekstrak objek JSON yang valid dari respons: {raw[:100]}...")


@dataclass
class VisionQcEvaluation:
    """Structured result of automated vision QC."""
    passed: bool
    confidence: float
    issues: List[str] = field(default_factory=list)
    notes: Optional[str] = None
    model_id: str = "unknown"
    checked_at: str = ""
    anatomy: Dict[str, Any] = field(default_factory=dict)
    disclaimer: str = VISION_QC_DISCLAIMER
    raw_analysis: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "confidence": self.confidence,
            "issues": self.issues,
            "notes": self.notes,
            "model": self.model_id,
            "checked_at": self.checked_at,
            "anatomy": self.anatomy,
            "disclaimer": self.disclaimer,
            "raw_analysis": self.raw_analysis,
        }


@dataclass
class MetadataGenerationResult:
    """Structured outcome of metadata generation."""
    title: str
    keywords: List[str]
    category_id: Optional[int]
    content_type: str = "photo"
    requires_manual_review: bool = False
    review_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "keywords": self.keywords,
            "category_id": self.category_id,
            "category_name": ADOBE_CATEGORIES.get(self.category_id or 0, "Unknown"),
            "content_type": self.content_type,
            "requires_manual_review": self.requires_manual_review,
            "review_reasons": self.review_reasons,
        }


class VisionQcService:
    """Service managing vision-based quality control and stock metadata generation (SF-010)."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        storage_service: StorageService,
        router_or_provider: Union[ProviderRouter, VisionProvider],
    ) -> None:
        self.session_factory = session_factory
        self.storage_service = storage_service
        self._router: Optional[ProviderRouter] = None
        self._provider: Optional[VisionProvider] = None

        if isinstance(router_or_provider, ProviderRouter):
            self._router = router_or_provider
        else:
            self._provider = router_or_provider

    def _resolve_provider_and_model(self, role: RoleType, model_override: Optional[str] = None) -> Tuple[VisionProvider, str]:
        if self._router:
            model_id = model_override or self._router.resolve_model(role)
            provider = self._router.registry.get_provider_for_model(model_id)
            if not isinstance(provider, VisionProvider):
                raise TypeError(f"Provider untuk model {model_id} tidak mengimplementasikan VisionProvider")
            return provider, model_id
        elif self._provider:
            return self._provider, (model_override or "default-vision-model")
        raise RuntimeError("VisionQcService tidak memiliki Router atau Provider yang terkonfigurasi")

    async def inspect_image_vision(
        self,
        asset_id: int,
        model_override: Optional[str] = None,
        safe_fallback: bool = True,
    ) -> QcResult:
        """Run vision model inspection on asset image bytes and persist QcResult.
        
        Guarantees:
        - Structured evaluation with issues list and confidence score.
        - Persists QcResult record under check_name='vision_qc'.
        - Includes legal disclaimer.
        - Graceful recovery: malformed responses do not crash the pipeline.
        """
        with self.session_factory() as session:
            asset = session.get(Asset, asset_id)
            if not asset:
                raise ValueError(f"Asset #{asset_id} tidak ditemukan di database")

            image_path = Path(asset.raw_path)
            if not image_path.exists():
                raise FileNotFoundError(f"File gambar fisik tidak ditemukan: {image_path}")

            image_bytes = image_path.read_bytes()

        provider, model_id = self._resolve_provider_and_model(RoleType.VISION_QC, model_override)
        now_iso = utc_now().isoformat()

        prompt = (
            "You are a rigorous commercial stock photography Quality Control (QC) inspector. "
            "Inspect this image with extreme scrutiny across these mandatory inspection pillars:\n"
            "1. ANATOMY & HUMAN SUBJECTS (HIGHEST PRIORITY):\n"
            "   - Hands & Fingers: Count the exact number of fingers on each visible hand. Every visible hand MUST have exactly 5 distinct, well-defined fingers. Flag any extra fingers (polydactyly), missing fingers, fused/webbed digits, unnatural joints, floating thumbs, or claw-like distortions.\n"
            "   - Hand Shape & Posture: Check for unnatural wrist bends, impossible finger contortions, or melting hand geometry.\n"
            "   - Facial Coherence: Check for facial symmetry, coherent eye pupils and gaze, natural eyelids, realistic teeth, and natural mouth/nose structure. Flag any asymmetric melting eyes, uncanny valley artifacts, extra teeth, or warped features.\n"
            "   - Human Interactions & Contact Boundaries: Check all points where hands touch objects (holding cups, tools, plants, phones, food, etc.) or touch other people. Flag any fingers disappearing into objects, objects melting into skin, overlapping hands blending into each other, or clipping geometry.\n"
            "2. ANTI-PLAGIARISM & IP CLEARANCE (STRICT ZERO TOLERANCE):\n"
            "   - Check for visible brand logos, trademarks, corporate symbols, copyrighted fictional characters, celebrity likenesses, watermarks, text overlays, or artist signatures.\n"
            "3. TECHNICAL DEFECTS:\n"
            "   - Check for severe blur, pixelation, severe noise, unnatural AI artifacts, or floating disjointed objects.\n\n"
            "CRITICAL FAIL-CLOSED RULE:\n"
            "If ANY anatomical defect (hand/finger count, distorted face, flawed interaction boundary), trademark/logo, watermark, or significant technical defect is present, "
            "you MUST mark 'passed': false, list the specific issues in 'issues', provide an anatomy breakdown, and explain in 'notes'.\n\n"
            "Return valid JSON in this exact structure:\n"
            "{\n"
            "  \"passed\": bool,\n"
            "  \"confidence\": float (0.0 to 1.0),\n"
            "  \"anatomy\": {\n"
            "    \"has_humans\": bool,\n"
            "    \"hands_and_fingers_defect\": bool,\n"
            "    \"face_defect\": bool,\n"
            "    \"interaction_defect\": bool,\n"
            "    \"details\": str\n"
            "  },\n"
            "  \"issues\": [str],\n"
            "  \"notes\": str\n"
            "}"
        )

        try:
            resp: ProviderVisionResponse = await provider.analyze_image(
                image_bytes=image_bytes,
                prompt=prompt,
                model=model_id,
                role=RoleType.VISION_QC,
            )
            analysis_dict = safe_extract_json(resp.analysis)

            passed = bool(analysis_dict.get("passed", resp.passed))
            confidence = float(analysis_dict.get("confidence", 0.9))
            issues = analysis_dict.get("issues", [])
            if not isinstance(issues, list):
                issues = [str(issues)]
            notes = str(analysis_dict.get("notes", ""))
            anatomy_dict = analysis_dict.get("anatomy", {})
            if not isinstance(anatomy_dict, dict):
                anatomy_dict = {"details": str(anatomy_dict)}

            # Strict anatomy detection guardrails (Fail-Closed)
            has_anatomy_defect = (
                bool(anatomy_dict.get("hands_and_fingers_defect"))
                or bool(anatomy_dict.get("face_defect"))
                or bool(anatomy_dict.get("interaction_defect"))
            )

            # Also check textual mentions of anatomical defects in issues or notes
            combined_text = ("; ".join(str(i) for i in issues) + " " + notes).lower()
            anatomy_defect_keywords = [
                "polydactyly", "extra finger", "missing finger", "6 fingers", "six fingers", "4 fingers", "four fingers",
                "fused finger", "webbed finger", "distorted hand", "distorted face", "melting finger", "clipping hand",
                "anatomical distortion", "deformed hand", "deformed finger"
            ]
            for kw in anatomy_defect_keywords:
                if kw in combined_text:
                    has_anatomy_defect = True
                    if not any(kw in str(iss).lower() for iss in issues):
                        issues.append(f"anatomical_defect_{kw.replace(' ', '_')}")

            if has_anatomy_defect:
                passed = False
                if anatomy_dict.get("hands_and_fingers_defect") and "anatomical_defect_hands_or_fingers" not in issues:
                    issues.append("anatomical_defect_hands_or_fingers")
                if anatomy_dict.get("face_defect") and "anatomical_defect_face" not in issues:
                    issues.append("anatomical_defect_face")
                if anatomy_dict.get("interaction_defect") and "anatomical_defect_interaction" not in issues:
                    issues.append("anatomical_defect_interaction")

            qc_eval = VisionQcEvaluation(
                passed=passed,
                confidence=confidence,
                issues=[str(i) for i in issues],
                notes=notes,
                model_id=model_id,
                checked_at=now_iso,
                anatomy=anatomy_dict,
                raw_analysis=analysis_dict,
            )
        except Exception as e:
            logger.error("Vision QC gagal untuk Asset #%d: %s", asset_id, e)
            if not safe_fallback:
                raise
            qc_eval = VisionQcEvaluation(
                passed=False,
                confidence=0.0,
                issues=["vision_qc_processing_error", str(e)],
                notes=f"Fail-closed: evaluasi model gagal ({e}). Gambar tidak diloloskan.",
                model_id=model_id,
                checked_at=now_iso,
                anatomy={"has_humans": False, "hands_and_fingers_defect": False, "face_defect": False, "interaction_defect": False, "details": f"Error: {e}"},
                raw_analysis={"error": str(e)},
            )

        with self.session_factory() as session:
            # Enforce Fail-Closed asset status transition
            asset_rec = session.get(Asset, asset_id)
            if asset_rec:
                if not qc_eval.passed:
                    asset_rec.status = "qc_failed"
                else:
                    # Only mark qc_passed if all deterministic checks also passed
                    det_failed = session.scalar(
                        select(QcResult.id).where(
                            QcResult.asset_id == asset_id,
                            QcResult.check_name != "vision_qc",
                            QcResult.passed == False,
                        )
                    )
                    if not det_failed:
                        asset_rec.status = "qc_passed"
                    else:
                        asset_rec.status = "qc_failed"

            qc_record = QcResult(
                asset_id=asset_id,
                check_name="vision_qc",
                passed=qc_eval.passed,
                score=qc_eval.confidence,
                details_json=json.dumps(qc_eval.to_dict()),
                checked_at=utc_now(),
            )
            session.add(qc_record)
            session.commit()
            session.refresh(qc_record)
            return qc_record

    async def generate_metadata(
        self,
        asset_id: int,
        model_override: Optional[str] = None,
        safe_fallback: bool = True,
    ) -> AssetMetadata:
        """Generate compliant stock metadata (title <= 70 chars, keywords <= 40, category 1-21).
        
        Guarantees:
        - Strict length cap on title (<= 70 chars) with word-boundary truncation.
        - Strict keyword cap (<= 40 items) with anti-hallucination / trademark filtering.
        - Validated Adobe Stock category ID (1-21).
        - Persists to asset_metadata table.
        - Graceful recovery: malformed outputs produce safe placeholder metadata flagged for review.
        """
        with self.session_factory() as session:
            asset = session.get(Asset, asset_id)
            if not asset:
                raise ValueError(f"Asset #{asset_id} tidak ditemukan di database")

            image_path = Path(asset.raw_path)
            if not image_path.exists():
                raise FileNotFoundError(f"File gambar fisik tidak ditemukan: {image_path}")

            image_bytes = image_path.read_bytes()

        provider, model_id = self._resolve_provider_and_model(RoleType.METADATA, model_override)

        prompt = (
            "Analyze this stock photograph and generate commercial metadata for Adobe Stock. "
            "Output JSON with: title (concise, <= 70 characters), keywords (20-40 keywords, "
            "strictly relevant and visible, ordered by relevance, no brand names), category_id (1-21), "
            "content_type ('photo')."
        )

        requires_review = False
        review_reasons: List[str] = []

        try:
            resp: ProviderVisionResponse = await provider.analyze_image(
                image_bytes=image_bytes,
                prompt=prompt,
                model=model_id,
                role=RoleType.METADATA,
            )
            analysis_dict = safe_extract_json(resp.analysis)

            raw_title = analysis_dict.get("title")
            raw_keywords = analysis_dict.get("keywords")
            raw_category = analysis_dict.get("category_id")
            content_type = str(analysis_dict.get("content_type", "photo")).lower()

            final_title = clean_title(raw_title, max_len=70)
            final_keywords = clean_keywords(raw_keywords, max_count=40)
            final_category = validate_category_id(raw_category)

            if len(final_keywords) < 5:
                requires_review = True
                review_reasons.append("Jumlah keyword kurang dari batas minimum (5)")

            if not final_category:
                requires_review = True
                review_reasons.append("Kategori Adobe Stock tidak valid atau tidak dapat ditentukan")

            if raw_title and len(str(raw_title)) > 70:
                review_reasons.append("Judul asli melebihi 70 karakter dan dipotong secara aman")

        except Exception as e:
            logger.error("Metadata generation gagal untuk Asset #%d: %s", asset_id, e)
            if not safe_fallback:
                raise
            final_title = f"Stock Asset #{asset_id}"
            final_keywords = ["commercial", "stock", "photo", "asset"]
            final_category = 8  # Default Graphic Resources / General
            content_type = "photo"
            requires_review = True
            review_reasons.append(f"Fallback otomatis akibat kesalahan model: {e}")

        meta_result = MetadataGenerationResult(
            title=final_title,
            keywords=final_keywords,
            category_id=final_category,
            content_type=content_type,
            requires_manual_review=requires_review,
            review_reasons=review_reasons,
        )

        with self.session_factory() as session:
            existing = session.get(AssetMetadata, asset_id)
            if existing:
                existing.title = meta_result.title
                existing.keywords_json = json.dumps(meta_result.keywords)
                existing.category_id = meta_result.category_id
                existing.content_type = meta_result.content_type
                existing.metadata_reviewed_at = None
                meta_record = existing
            else:
                meta_record = AssetMetadata(
                    asset_id=asset_id,
                    title=meta_result.title,
                    keywords_json=json.dumps(meta_result.keywords),
                    category_id=meta_result.category_id,
                    content_type=meta_result.content_type,
                    metadata_reviewed_at=None,
                )
                session.add(meta_record)

            session.commit()
            session.refresh(meta_record)
            logger.info(
                "Metadata berhasil disimpan untuk Asset #%d: '%s' (%d keywords, category=%s, needs_review=%s)",
                asset_id,
                meta_record.title,
                len(meta_result.keywords),
                meta_record.category_id,
                requires_review,
            )
            return meta_record

