from kb_core.adapters.base import PushDecoder
from kb_core.adapters.webhook.decoder import WebhookDecoder


def test_decoder_satisfies_protocol():
    assert isinstance(WebhookDecoder(), PushDecoder)


def test_decode_maps_payload_to_record():
    rec = WebhookDecoder().decode({
        "external_id": "evt-1", "title": "Hello World",
        "content": "  body  ", "parent_path": "Inbox", "tags": ["x"],
    })
    assert rec.source == "webhook" and rec.external_id == "evt-1"
    assert rec.type == "note" and rec.content == "body"
    assert rec.parent_path == "Inbox" and rec.slug == "Hello_World"
    assert rec.tags == ["x"]


def test_decode_content_falls_back_to_title_then_id():
    assert WebhookDecoder().decode({"external_id": "e"}).content == "e"
    assert WebhookDecoder().decode({"external_id": "e", "title": "T"}).content == "T"
