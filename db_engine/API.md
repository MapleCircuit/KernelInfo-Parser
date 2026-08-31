# Database Engine Subsystem API Specification

The Database Engine subsystem provides low-level SQL and in-memory persistence drivers implementing connection pooling, batch DML statements, parallel table commits, and DDL schema/index management.

## Authoritative Documentation
For exhaustive architectural specifications, driver contracts, query chunking rules, and method signatures, refer to:
- [`DB_API.md`](db_engine/DB_API.md)

## Core Components & Drivers

| Component | File | Primary Responsibility |
| :--- | :--- | :--- |
| `BaseDBEngine` | [`base.py`](db_engine/base.py) | Abstract base class interface defining the full database backend contract. |
| `MariaDB` | [`DBHandling.py`](db_engine/DBHandling.py) | Production MySQL / MariaDB direct driver utilizing parameterized SQL execution, batch chunking, socket reconnect guards, and parallel table commit workers. |
| `MockDB` | [`mock_db.py`](db_engine/mock_db.py) | In-memory mock database engine with relational join evaluation for isolated, ultra-fast unit testing. |
| `get_db_engine(name)` | [`__init__.py`](db_engine/__init__.py) | Factory function mapping driver aliases (`"mariadb"`, `"mock"`) to driver classes. |

