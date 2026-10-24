/** Quote a column name for SQL when it is not a plain identifier. */
export const quoteIdent = (name: string) =>
  /^[a-z_][a-z0-9_]*$/i.test(name) ? name : `"${name.replace(/"/g, '""')}"`;
