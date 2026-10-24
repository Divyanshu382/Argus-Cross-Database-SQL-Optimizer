"""External databases registered with the shared DuckDB connection.

SQL sources are ATTACHed read-only, so DuckDB queries them where they live and
can join them with each other and with local tables in one statement. MongoDB
has no DuckDB scanner; its collections are queried live through
backend.db.mongo.

Credentials are held only in this process's memory and never written to disk,
so connections have to be made again after a restart.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal
from urllib.parse import unquote, urlparse

from backend.db import mongo
from backend.db.duckdb import get_connection, register_before_query, split_relation
from backend.db.duckdb import quote_ident as _ident
from backend.db.duckdb import sql_literal as _sql_literal

Kind = Literal["postgres", "mysql", "mongodb"]

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
_RESERVED = {"main", "memory", "system", "temp", "information_schema", "pg_catalog", "aetherquery"}
_DEFAULT_PORTS = {"postgres": 5432, "mysql": 3306, "mongodb": 27017}

# Public read-only databases that are handy for trying federation out. The
# credentials are the ones their owners (EMBL-EBI) publish for anonymous use.
PUBLIC_DATABASES = [
    {
        "name": "rfam",
        "kind": "mysql",
        "label": "Rfam (MySQL): RNA families, EMBL-EBI public database",
        "host": "mysql-rfam-public.ebi.ac.uk",
        "port": 4497,
        "database": "Rfam",
        "user": "rfamro",
        "password": "",
    },
    {
        "name": "rnacentral",
        "kind": "postgres",
        "label": "RNAcentral (PostgreSQL): non-coding RNA, EMBL-EBI public database",
        "host": "hh-pgsql-public.ebi.ac.uk",
        "port": 5432,
        "database": "pfmegrnargs",
        "user": "reader",
        "password": "NWDMCE5xdipIjRrp",
        "schema_name": "rnacen",
    },
]


@dataclass
class ConnectionParams:
    kind: Kind
    host: str = ""
    port: int | None = None
    database: str = ""
    user: str = ""
    password: str = ""
    uri: str | None = None
    schema_name: str | None = None

    @classmethod
    def from_uri(cls, kind: Kind, uri: str, **extra: Any) -> "ConnectionParams":
        parsed = urlparse(uri)
        return cls(
            kind=kind,
            host=parsed.hostname or "",
            port=parsed.port,
            database=unquote(parsed.path.lstrip("/")),
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            uri=uri,
            **extra,
        )

    @property
    def target(self) -> str:
        """Where the connection points, for display; never includes the password."""
        port = f":{self.port}" if self.port else ""
        database = f"/{self.database}" if self.database else ""
        user = f"{self.user}@" if self.user else ""
        return f"{user}{self.host}{port}{database}"


@dataclass
class Connection:
    name: str
    kind: str
    target: str
    # "attached": queried in place; "passthrough": queried in place through
    # plain SELECTs; "live": MongoDB, queried per query (see backend.db.mongo).
    mode: str
    tables: list[str]
    note: str | None = None
    connected_at: float = field(default_factory=time.time)
    # How to send SQL straight to the source (SQL sources only): the DuckDB
    # catalog to pass to postgres_query/mysql_query, and the schema a
    # two-part name such as rfam.family refers to.
    query_alias: str | None = None
    default_schema: str | None = None

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        del data["query_alias"], data["default_schema"]
        return data


@dataclass(frozen=True)
class RemoteTable:
    """A table in a connected SQL database, as the source itself names it."""

    connection: str
    kind: str
    alias: str
    schema: str
    table: str

    @property
    def source_name(self) -> str:
        if self.kind == "mysql":
            return ".".join("`" + part.replace("`", "``") + "`" for part in (self.schema, self.table))
        return ".".join('"' + part.replace('"', '""') + '"' for part in (self.schema, self.table))

    def query_sql(self, sql: str) -> str:
        """DuckDB SQL that runs ``sql`` (in the source's dialect) on the source."""
        function = "postgres_query" if self.kind == "postgres" else "mysql_query"
        return f"SELECT * FROM {function}({_sql_literal(self.alias)}, {_sql_literal(sql)})"


_connections: dict[str, Connection] = {}
# Passthrough views not created yet: connection name -> {table: CREATE VIEW sql}.
_pending_views: dict[str, dict[str, str]] = {}
_lock = threading.RLock()


# ---------------------------------------------------------------------------
# quoting helpers
# ---------------------------------------------------------------------------


def _dsn_value(value: str) -> str:
    # libpq-style key=value strings need quoting for empty values and spaces.
    if value and re.fullmatch(r"[^\s'\\]+", value):
        return value
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _dsn(params: ConnectionParams) -> str:
    port = params.port or _DEFAULT_PORTS[params.kind]
    pairs = {"host": params.host, "port": str(port), "user": params.user}
    pairs["dbname" if params.kind == "postgres" else "database"] = params.database
    if params.password:
        pairs["password"] = params.password
    return " ".join(f"{key}={_dsn_value(value)}" for key, value in pairs.items() if value)


def _scrub(message: str, params: ConnectionParams) -> str:
    """Keep passwords out of error messages that echo the connection string."""
    if params.password:
        message = message.replace(params.password, "***")
    return message


# ---------------------------------------------------------------------------
# SQL sources
# ---------------------------------------------------------------------------


def _list_source_tables(name: str, params: ConnectionParams, default_schema: str) -> list[str]:
    """Tables of an attached database, as the source's information_schema lists them.

    DuckDB's own listings (duckdb_tables()) would also read every other
    attached catalog, and fail if any of them denies catalog access.
    """
    if params.kind == "mysql":
        function = "mysql_query"
        condition = (
            f"table_schema = {_sql_literal(params.database)}"
            if params.database
            else "table_schema NOT IN ('mysql', 'sys', 'performance_schema', 'information_schema')"
        )
    else:
        function = "postgres_query"
        condition = (
            f"table_schema = {_sql_literal(params.schema_name)}"
            if params.schema_name
            else "table_schema NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"
        )
    listing = (
        f"SELECT table_schema, table_name FROM information_schema.tables WHERE {condition} "
        "ORDER BY table_schema, table_name"
    )
    rows = get_connection().execute(
        f"SELECT * FROM {function}({_sql_literal(name)}, {_sql_literal(listing)})"
    ).fetchall()
    tables = []
    for schema, table in rows:
        # Both servers type these columns as identifiers that arrive as bytes.
        schema, table = (v.decode() if isinstance(v, bytes) else v for v in (schema, table))
        # The catalog's default schema can be left out of the name.
        parts = [name, table] if schema == default_schema else [name, schema, table]
        tables.append(".".join(_ident(part) for part in parts))
    return tables


def _connect_passthrough(name: str, params: ConnectionParams, dsn: str) -> tuple[list[str], str]:
    """Expose a Postgres schema through postgres_query() views.

    Some servers (public read replicas especially) deny the catalog reads that
    DuckDB's scanner runs on first use, while still allowing ordinary queries.
    Each table becomes a view that runs a plain SELECT on the server. Creating
    a view asks the server for its columns (a second or two over a long link),
    so views are only created when a query first mentions them.
    """
    conn = get_connection()
    raw = f"__raw_{name}"
    schema = params.schema_name or "public"
    conn.execute(f"ATTACH {_sql_literal(dsn)} AS {raw} (TYPE postgres, READ_ONLY)")
    listing_sql = (
        "SELECT table_name::text FROM information_schema.tables "
        f"WHERE table_schema = {_sql_literal(schema)} ORDER BY table_name"
    )
    names = [
        row[0]
        for row in conn.execute(
            f"SELECT * FROM postgres_query({_sql_literal(raw)}, {_sql_literal(listing_sql)})"
        ).fetchall()
    ]
    conn.execute(f"ATTACH ':memory:' AS {name}")
    pending: dict[str, str] = {}
    for table in names:
        remote = f'"{schema.replace(chr(34), chr(34) * 2)}"."{table.replace(chr(34), chr(34) * 2)}"'
        pending[table.lower()] = (
            f"CREATE VIEW {name}.main.{_ident(table)} AS SELECT * FROM "
            f"postgres_query({_sql_literal(raw)}, {_sql_literal(f'SELECT * FROM {remote}')})"
        )
    _pending_views[name] = pending
    note = (
        "This server blocks the catalog access DuckDB normally uses, so tables are read with "
        "plain SELECTs: filters run after the whole table is downloaded. Prefer small tables."
    )
    return [f"{name}.{_ident(table)}" for table in names], note


def resolve_remote(relation: str) -> RemoteTable | None:
    """The source table behind <connection>.<table>, for connected SQL databases."""
    parts = split_relation(relation)
    if len(parts) not in (2, 3):
        return None
    connection = _connections.get(parts[0].lower())
    if connection is None or connection.query_alias is None:
        return None
    schema = parts[1] if len(parts) == 3 else connection.default_schema
    return RemoteTable(connection.name, connection.kind, connection.query_alias, schema or "", parts[-1])


def _connect_sql(name: str, params: ConnectionParams) -> Connection:
    conn = get_connection()
    kind = params.kind
    conn.execute(f"INSTALL {kind}; LOAD {kind};")
    dsn = _dsn(params)
    options = f"TYPE {kind}, READ_ONLY"
    if kind == "postgres" and params.schema_name:
        options += f", SCHEMA {_sql_literal(params.schema_name)}"
    conn.execute(f"ATTACH {_sql_literal(dsn)} AS {name} ({options})")

    default_schema = "public" if kind == "postgres" else params.database
    try:
        tables = _list_source_tables(name, params, default_schema)
        if tables:
            # ATTACH is lazy: this is the first read of DuckDB's catalog for it,
            # which is what locked-down servers refuse.
            conn.execute(f"SELECT * FROM {tables[0]} LIMIT 0")
        return Connection(
            name, kind, params.target, "attached", tables,
            query_alias=name, default_schema=params.schema_name or default_schema,
        )
    except Exception as exc:
        conn.execute(f"DETACH {name}")
        if kind != "postgres" or "permission denied" not in str(exc).lower():
            raise
    tables, note = _connect_passthrough(name, params, dsn)
    return Connection(
        name, kind, params.target, "passthrough", tables, note,
        query_alias=f"__raw_{name}", default_schema=params.schema_name or "public",
    )


# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------


def _connect_mongodb(
    name: str,
    params: ConnectionParams,
    client_factory: Callable[..., Any] | None = None,
) -> Connection:
    uri = params.uri or f"mongodb://{params.host}:{params.port or _DEFAULT_PORTS['mongodb']}"
    tls: dict[str, Any] = {}
    if client_factory is None:
        from pymongo import MongoClient

        client_factory = MongoClient
        uses_tls = uri.startswith("mongodb+srv://") or re.search(r"[?&](tls|ssl)=true", uri, re.IGNORECASE)
        if uses_tls:
            try:
                import certifi

                # python.org builds of Python ship without root certificates, so
                # TLS to Atlas fails unless a CA bundle is named explicitly.
                tls = {"tlsCAFile": certifi.where()}
            except ImportError:
                pass

    auth = {} if params.uri or not params.user else {"username": params.user, "password": params.password}
    if not params.database:
        raise ValueError("Name the MongoDB database to read (in the URI path or the database field).")
    client = client_factory(uri, serverSelectionTimeoutMS=10_000, **auth, **tls)
    try:
        tables, note = mongo.connect(name, client, params.database)
    except Exception:
        client.close()
        raise
    return Connection(name, "mongodb", params.target, "live", tables, note)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def connect(
    name: str,
    params: ConnectionParams,
    client_factory: Callable[..., Any] | None = None,
) -> Connection:
    if not _NAME.match(name) or name in _RESERVED:
        raise ValueError(
            "Connection name must start with a letter and use only lowercase letters, digits "
            "and _ (max 31 characters)."
        )
    with _lock:
        if name in _connections:
            raise ValueError(f"A connection named '{name}' already exists. Disconnect it first.")
        try:
            if params.kind == "mongodb":
                connection = _connect_mongodb(name, params, client_factory)
            else:
                connection = _connect_sql(name, params)
        except Exception as exc:
            raise ValueError(_scrub(str(exc), params)) from None
        _connections[name] = connection
        return connection


def _create_pending_views(sql: str) -> None:
    """Create the passthrough views that ``sql`` mentions as <connection>.<table>."""
    if not _pending_views:
        return
    with _lock:
        for name, pending in _pending_views.items():
            if not pending:
                continue
            pattern = rf'(?i)\b{name}\s*\.\s*("(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_]*)'
            for match in re.finditer(pattern, sql):
                table = match.group(1)
                if table.startswith('"'):
                    table = table[1:-1].replace('""', '"')
                create_sql = pending.get(table.lower())
                if create_sql is not None:
                    get_connection().execute(create_sql)
                    del pending[table.lower()]


register_before_query(_create_pending_views)


def disconnect(name: str) -> None:
    with _lock:
        if _connections.pop(name, None) is None:
            raise KeyError(name)
        _pending_views.pop(name, None)
        conn = get_connection()
        conn.execute(f"DETACH DATABASE IF EXISTS {name}")
        conn.execute(f"DETACH DATABASE IF EXISTS __raw_{name}")
        mongo.disconnect(name)


def list_connections() -> list[Connection]:
    return list(_connections.values())
