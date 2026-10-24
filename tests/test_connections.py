import pytest

from backend.core.parser import parse_analytical_query
from backend.db import connections as reg


def test_dsn_quotes_awkward_values_and_errors_hide_the_password():
    params = reg.ConnectionParams(kind="postgres", host="h", user="me", password="p a'ss", database="d")
    assert "password='p a\\'ss'" in reg._dsn(params)
    assert reg._scrub("failed: password=p a'ss", params) == "failed: password=***"


def test_split_relation_handles_quotes():
    assert reg.split_relation('rfam."Family"') == ["rfam", "Family"]
    assert reg.split_relation('a."b.c"') == ["a", "b.c"]


def test_quote_ident_quotes_reserved_words():
    # sample_mflix.movies has a field called "cast".
    from backend.db.duckdb import quote_ident

    assert quote_ident("cast") == '"cast"'
    assert quote_ident("year") == "year"
    assert quote_ident("pickup zone") == '"pickup zone"'


def test_parser_accepts_qualified_table_names():
    assert parse_analytical_query("SELECT COUNT(*) FROM rfam.family").table == "rfam.family"
    assert parse_analytical_query('SELECT COUNT(*) FROM pg.public."Orders"').table == 'pg.public."Orders"'


def test_connection_names_are_validated():
    with pytest.raises(ValueError, match="lowercase"):
        reg.connect("Bad-Name", reg.ConnectionParams(kind="mysql", host="x"))


def test_flattening_nested_documents():
    from backend.db.mongo import flatten_documents

    frame = flatten_documents(
        [
            {"a": {"b": 1}, "tags": ["x"], "mixed": 1},
            {"a": {"b": 2}, "tags": [], "mixed": "text"},
        ]
    )
    assert list(frame["a_b"]) == [1, 2]
    assert list(frame["tags"]) == ['["x"]', "[]"]
    assert list(frame["mixed"]) == ["1", "text"]  # mixed types become text
