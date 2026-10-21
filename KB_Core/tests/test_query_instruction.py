"""instruct_query: the exact query instruction the CONFIDENCE_* pair was derived with."""
from kb_core.embedding import (QUERY_INSTRUCTION, QUERY_INSTRUCTION_VERSION,
                               instruct_query)


def test_instruction_is_byte_exact():
    # Changing ANY byte -- the space after "Query:" included -- invalidates the
    # CONFIDENCE_* pair: bump QUERY_INSTRUCTION_VERSION and re-derive them.
    assert QUERY_INSTRUCTION == ("Instruct: Given a web search query, retrieve relevant "
                                 "passages that answer the query\nQuery: {q}")
    assert QUERY_INSTRUCTION_VERSION == "instructed-v1"


def test_instruct_query_formats_the_query():
    assert instruct_query("pgvector rebuild") == (
        "Instruct: Given a web search query, retrieve relevant passages that answer "
        "the query\nQuery: pgvector rebuild")


def test_braces_in_a_query_stay_literal():
    assert instruct_query("{q} {0} }{").endswith("Query: {q} {0} }{")


def test_a_blank_query_has_no_instructed_form():
    # An instructed blank is never blank: it would embed the instruction alone and
    # return real rows with real cosines.
    for blank in ("", "   ", "\n\t"):
        assert instruct_query(blank) is None


def test_clip_order_is_immaterial():
    q = "x" * 7000
    assert instruct_query(q[:6000])[:6000] == instruct_query(q)[:6000]
