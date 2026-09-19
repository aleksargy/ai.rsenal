"""Squad optimisation: the integer program and its candidate pool."""

from .baseline import availability, baseline_xp, build_candidates
from .model import (
    Candidate,
    GameweekDecision,
    InfeasibleError,
    OptimiserConfig,
    OptimiserError,
    Plan,
    optimise,
)

__all__ = [
    "Candidate",
    "GameweekDecision",
    "InfeasibleError",
    "OptimiserConfig",
    "OptimiserError",
    "Plan",
    "availability",
    "baseline_xp",
    "build_candidates",
    "optimise",
]
