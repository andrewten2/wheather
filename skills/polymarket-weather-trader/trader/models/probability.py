from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class ProbabilityEstimate:
    model_name: str
    model_version: str
    estimated_probability: float
    edge: float
    confidence: float
    reasoning: str
    metadata: Dict[str, Any] = field(default_factory=dict)

