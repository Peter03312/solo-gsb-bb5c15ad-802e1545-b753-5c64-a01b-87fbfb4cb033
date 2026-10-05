#!/usr/bin/env python3
"""初始化数据库：建表 + 写入种子政策版本与示例节假日表。幂等，可重复执行。

用法：
    CLAIMS_DB_PATH=/data/claims.db python scripts/init_db.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.seed import ensure_initialized  # noqa: E402
from app.storage import Store  # noqa: E402


def main() -> None:
    path = os.environ.get("CLAIMS_DB_PATH", "claims.db")
    store = Store(path)
    ensure_initialized(store)
    stats = store.stats()
    print(f"[init] 数据库已就绪: {path}")
    for table, count in stats.items():
        print(f"[init]   {table}: {count} 行")
    print(f"[init] 政策版本: {[p['version'] for p in store.list_policies()]}")
    print(f"[init] 节假日表: {store.list_calendars()}")


if __name__ == "__main__":
    main()
