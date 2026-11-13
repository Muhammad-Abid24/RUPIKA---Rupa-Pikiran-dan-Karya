"""StockForge / RUPIKA Research, Planning, and Generation Gate Module (SF-006, SF-020, SF-022)."""
from stockforge.research.cron import format_schedule_key, is_cron_match, parse_cron_field
from stockforge.research.gate import (
    GenerationGate,
    GenerationGateBlockedError,
    PlanAlreadyConsumedError,
    PlanExpiredError,
)
from stockforge.research.models import (
    CommercialConceptData,
    compute_concept_fingerprint,
    normalize_prompt_text,
)
from stockforge.research.planner import (
    PlanIntegrityError,
    PlanStateError,
    ProductionPlanner,
    compute_plan_hash,
)
from stockforge.research.scheduler import AutoResearchScheduler
from stockforge.research.service import (
    ConceptApprovalError,
    DuplicateConceptError,
    ResearchResult,
    ResearchService,
)
from stockforge.research.settings import (
    ResearchBudgetExceededError,
    ResearchSettingsError,
    ResearchSettingsManager,
)

__all__ = [
    "CommercialConceptData",
    "compute_concept_fingerprint",
    "normalize_prompt_text",
    "ResearchResult",
    "ResearchService",
    "ConceptApprovalError",
    "DuplicateConceptError",
    "ResearchSettingsManager",
    "ResearchSettingsError",
    "ResearchBudgetExceededError",
    "ProductionPlanner",
    "PlanStateError",
    "PlanIntegrityError",
    "compute_plan_hash",
    "GenerationGate",
    "GenerationGateBlockedError",
    "PlanAlreadyConsumedError",
    "PlanExpiredError",
    "AutoResearchScheduler",
    "is_cron_match",
    "parse_cron_field",
    "format_schedule_key",
]
