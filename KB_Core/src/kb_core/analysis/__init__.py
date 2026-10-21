"""Background re-analysis — decoupled from ingestion.

Ingestion captures and enriches NEW data; this subsystem revisits EXISTING nodes and
redraws their connections (categories, entities, tags, importance) as the KB evolves. It
runs as its own process (kb-mcp-reanalyzed), on its own schedule, sharing only the
KnowledgeBase and the LLM enricher logic — it is not part of the ingestion pipeline.
"""
