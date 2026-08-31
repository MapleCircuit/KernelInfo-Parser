# TableEngine Subsystem API Specification

The TableEngine subsystem provides stateful caching, monotonic sequence tracking, relational view decomposition, and batch transaction management between `TableHandling` (ChangeSets) and `db_engine` (SQL/storage driver).

## Authoritative Documentation
For exhaustive architectural specifications, state models, view decomposition algorithms, and caching index rules, refer to:
- [`TE_API.md`](table_engine/TE_API.md)

## Core Components & Engine Implementations

| Component | File | Primary Responsibility |
| :--- | :--- | :--- |
| `get_table_engine(name)` | [`__init__.py`](table_engine/__init__.py) | Factory resolver mapping engine aliases (`"cached"`, `"direct"`) to implementation classes. |
| `TEDirectDB` | [`te_direct_db.py`](table_engine/te_direct_db.py) | Direct table engine providing monotonic sequencing, local insert/update staging, hash-accelerated view decomposition, and batch commit flushing. |
| `TECachedDB` | [`te_cached_db.py`](table_engine/te_cached_db.py) | High-performance in-memory cached table engine providing startup preloading, multi-index acceleration (`_pk_index`, `_nodup_index`, `_col_indices`), and zero-latency queries for `te_cached=True` tables. |
| `compute_ast_hash()` | [`te_direct_db.py`](table_engine/te_direct_db.py) | Computes deterministic SHA-256 hash for join graphs and column filters to deduplicate relational views. |

