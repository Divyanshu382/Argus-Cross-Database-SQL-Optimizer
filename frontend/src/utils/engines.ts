import type { SourceKind } from "../types";

export const ENGINE_LABEL: Record<SourceKind, string> = {
  local: "DuckDB",
  postgres: "PostgreSQL",
  mysql: "MySQL",
  mongodb: "MongoDB",
};

export const ENGINE_BADGE: Record<SourceKind, string> = {
  local: "duck",
  postgres: "pg",
  mysql: "my",
  mongodb: "mongo",
};
