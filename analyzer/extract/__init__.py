"""Document extraction: the half of ADR 0004 that is allowed to touch a network.

The split across this boundary is not cosmetic. `analyzer/core/redact.py` and
`analyzer/core/extraction.py` hold the redaction rules, the declared schema, and every
refusal -- and `tests/test_core_purity.py` forbids `openai`, `anthropic`, `ollama`, and
`httpx` from ever appearing there. So this package exists to hold the one thing core cannot:
the call itself.

The consequence worth stating, because it is what makes the feature testable: every rule that
determines whether a field is accepted runs with no network and no API key. The provider is
swappable and the offline one is deterministic, so `pytest` exercises the entire decision path
on a machine that has never heard of a model provider. That is why CI can prove the boundary
holds rather than asserting it in a markdown file.
"""

from .documents import Document, load_document
from .providers import (
    ExtractionProvider,
    OfflineProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    ProviderError,
    build_provider,
)
from .run import ExtractionRun, extract_from_document

__all__ = [
    "Document",
    "ExtractionProvider",
    "ExtractionRun",
    "OfflineProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "ProviderError",
    "build_provider",
    "extract_from_document",
    "load_document",
]
