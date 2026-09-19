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
from .pool import candidates_from_forecasts

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
    "candidates_from_forecasts",
    "optimise",
]
