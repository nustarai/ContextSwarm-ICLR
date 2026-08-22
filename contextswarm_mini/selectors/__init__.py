"""Pure selector implementations for the Issue #38 comparison surface."""

from ..selection import RandomSelector, RecencySelector, build_selector, selector_registry
from .feedback import (
    FeedbackDiversitySelector,
    NoInteractionSelector,
    NuStigmergySelector,
    UnnormalizedSelector,
)
from .popularity import SmoothedPopularitySelector
from .text import BM25MMRSelector

__all__ = [
    "BM25MMRSelector",
    "FeedbackDiversitySelector",
    "NoInteractionSelector",
    "NuStigmergySelector",
    "RandomSelector",
    "RecencySelector",
    "SmoothedPopularitySelector",
    "UnnormalizedSelector",
    "build_selector",
    "selector_registry",
]
