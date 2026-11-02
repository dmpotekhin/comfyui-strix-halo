"""Workflow discovery: find workflows and LoRAs, judge whether they can run here, keep the receipts.

Layout, and why it is split this way:

``model``      resource and provenance records, hashing, graph signatures
``sources``    the 24-source catalogue with priorities and honest capability flags
``net``        the single HTTP seam — user agent, timeouts, size caps, rate-limit reporting
``media``      containers workflows hide in: PNG ``tEXt``, WebP EXIF/XMP, ZIP
``classify``   format detection delegated to ``app.workflow_io`` (no second detector)
``store``      SQLite persistence, deduplication, merge rules, favourites, adaptations
``providers``  one adapter per source family, each reporting what it actually did
``sync``       the background runner that walks enabled sources one at a time
``analyze``    graph → nodes, models, categories, compatibility (via ``app.inventory``)
``embed``      embedding backends; semantic only if a model is already on disk
``index``      the vector cache, keyed by text hash so unchanged rows are skipped
``search``     hybrid ranking with bilingual query expansion and explanations
``rank``       optional loopback LLM reranking, strictly validated
``adapt``      typed replacement operations with a plan → confirm → validate flow

Nothing here executes a workflow, downloads a model, or installs a custom node.
"""

__all__ = [
    "model", "sources", "net", "media", "classify", "store", "providers", "sync",
    "analyze", "embed", "index", "search", "rank", "adapt",
]
