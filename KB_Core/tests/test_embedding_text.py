from kb_core.embedding import build_embed_text, embed_text_hash

def test_instance_text_includes_name_aliases_and_mentions():
    t = build_embed_text("Acme", ["Acme U"], "a research school",
                          [("Visited Acme", "had a tour")], kind="instance")
    assert "Acme" in t and "Acme U" in t and "a research school" in t
    assert "Visited Acme" in t and "had a tour" in t

def test_text_is_truncated_to_budget():
    big = [("t", "x" * 5000)]
    t = build_embed_text("N", [], None, big, kind="instance", budget=500)
    assert len(t) <= 500

def test_class_text_includes_member_names():
    t = build_embed_text("People", [], "humans", [], kind="class", members=["Avery", "Blake"])
    assert "Avery" in t and "Blake" in t

def test_hash_is_stable_and_sensitive():
    a = build_embed_text("N", [], None, [("t", "s")], kind="instance")
    b = build_embed_text("N", [], None, [("t", "s2")], kind="instance")
    assert embed_text_hash(a) == embed_text_hash(a)
    assert embed_text_hash(a) != embed_text_hash(b)
