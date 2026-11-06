# Changelog

## 0.1.0 - 2026-11-06

First release.

- Eight tools: `db_overview`, `describe_table`, `schema_graph`, `query`, `explain_query`,
  `find_in_db`, `data_quality`, `schema_docs`.
- Read-only by construction: `mode=ro` URI (plus `immutable=1` for a WAL-mode file with no
  pending WAL, so opening never creates `-wal` / `-shm`), `PRAGMA query_only`, `trusted_schema=OFF`,
  an authorizer that allows only SELECT / READ / function calls / a short list of read-only
  pragmas, a progress-handler deadline, and a 64 MiB value-length limit.
- Clear errors for empty files, non-SQLite files, probably-encrypted (SQLCipher) files, corrupt
  files and databases with a hot rollback journal.
- `--dir` confines the server to one folder (symlinks and `..` resolved before the check).
