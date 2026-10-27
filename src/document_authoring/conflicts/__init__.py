"""Deterministic cross-source conflict detection for document authoring."""

from .detector import ConflictDetector
from .models import ConflictCase, ConflictDecision, NormalizedClaim
from .normalizer import normalize_claim
from .policy import ConflictPolicy

__all__ = [
    "ConflictCase",
    "ConflictDecision",
    "ConflictDetector",
    "ConflictPolicy",
    "NormalizedClaim",
    "normalize_claim",
]
