from kb_core.registry import TypeRegistry, default_registry


def test_register_and_lookup():
    r = TypeRegistry()
    r.register("note")
    assert r.is_registered("note")
    assert not r.is_registered("email")
    assert r.extension_for("note") is None


def test_register_with_extension():
    r = TypeRegistry()
    r.register("email", extension="email_ext")
    assert r.extension_for("email") == "email_ext"


def test_default_registry_has_note_and_folder():
    r = default_registry()
    assert r.is_registered("note")
    assert r.is_registered("folder")
