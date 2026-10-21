"""WebhookDecoder: a generic JSON push payload -> NodeRecord (pure)."""

from kb_core.domain import NodeRecord
from kb_core.paths import sanitize_slug


class WebhookDecoder:
    """Payload contract: {external_id, title?, content?, parent_path?, slug?, tags?, uri?}."""
    source = "webhook"

    def decode(self, payload: dict) -> NodeRecord:
        external_id = str(payload["external_id"])
        title = payload.get("title")
        content = str(payload.get("content") or "").strip() or (title or external_id)
        slug = payload.get("slug") or title or external_id
        return NodeRecord(
            source=self.source, external_id=external_id, type="note",
            content=content, slug=sanitize_slug(str(slug)),
            parent_path=str(payload.get("parent_path", "")),
            title=title, tags=list(payload.get("tags", [])),
            uri=payload.get("uri"),
        )
