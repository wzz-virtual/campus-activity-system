"""V1.0 → V2.0 的数据库就地升级。

V2.0 改了数据模型（角色、活动状态、报名状态都换了取值，还添了几列）。
因为要求「网页里的数据不变」，这里**不重建数据库**，而是就地迁移：

1. 给 ``activity`` / ``enrollment`` 补上 V2.0 需要的新列；
2. 把旧取值翻译成新取值（CLUB→TEACHER 等），行数一条不少。

翻译规则（每一条都写清「为什么这么翻」）：

* ``user.role``：``CLUB`` → ``TEACHER``。V2.0 把「社团」角色改成了
  「活动组织教师」，同一个账号换个角色名，人和数据都不动。
* ``activity.status``：``PENDING`` → ``PUBLISHED``。V2.0 取消了活动审核流，
  教师直接发布——原本"等着管理员批"的活动视为已经发布。
  ``REJECTED`` → ``DRAFT``：被驳回说明内容需要修改，退回草稿由教师处理。
* ``enrollment.status``：``ENROLLED`` → ``CONFIRMED``（V2.0 里叫「正式」）。
  V1.0 没有候补，所以不存在需要翻译成候补的记录。

**幂等**：可以重复执行。检测到结构已经是 V2.0 时直接返回，不会改任何东西。

遗留列：V1.0 的 ``approved_by`` / ``approved_at`` / ``reject_reason`` 三列
会留在旧库里（SQLite 删列要重建整张表，风险与收益不成比例），新代码不再读写它们。
"""

from sqlalchemy import text

from .extensions import db

# 表名 -> [(新列名, 建列 DDL 片段)]
NEW_COLUMNS = {
    "activity": [
        ("requires_review", "BOOLEAN NOT NULL DEFAULT 0"),
        ("qualification", "VARCHAR(255)"),
    ],
    "enrollment": [
        ("waitlisted_at", "DATETIME"),
        ("reviewed_at", "DATETIME"),
        # 不加 REFERENCES 子句：SQLite 对 ALTER TABLE ADD COLUMN 带外键约束
        # 有额外限制，而这条约束对运行期查询没有影响（fresh 库由 create_all 建全）。
        ("reviewed_by", "INTEGER"),
        # V3.0（DR-4）：候补转正时刻，用于统计候补转正率
        ("promoted_at", "DATETIME"),
    ],
}

# 取值翻译：表.列 -> [(旧值, 新值)]
VALUE_MAP = {
    ("user", "role"): [("CLUB", "TEACHER")],
    ("activity", "status"): [("PENDING", "PUBLISHED"), ("REJECTED", "DRAFT")],
    ("enrollment", "status"): [("ENROLLED", "CONFIRMED")],
}


def _table_names(connection):
    rows = connection.execute(
        text("SELECT name FROM sqlite_master WHERE type='table'")
    ).fetchall()
    return {row[0] for row in rows}


def _columns(connection, table):
    rows = connection.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {row[1] for row in rows}


def schema_needs_upgrade():
    """当前数据库是否缺少模型需要的新列（V1.0 / V2.0 的老库都算）。

    不只看 ``activity.requires_review``：V3.0 又给 ``enrollment`` 加了
    ``promoted_at``，已经升到 V2.0 的库同样要再补一次。改成逐表逐列核对，
    以后再添列只要往 ``NEW_COLUMNS`` 里写一行就够了。
    """
    with db.engine.connect() as connection:
        tables = _table_names(connection)
        if "activity" not in tables:
            return False  # 还没有表——交给 create_all 按新模型建
        for table, columns in NEW_COLUMNS.items():
            if table not in tables:
                continue
            existing = _columns(connection, table)
            for name, _ddl in columns:
                if name not in existing:
                    return True
        return False


def upgrade_to_v2():
    """就地升级到 V2.0。返回一串「做了什么」的说明（已是最新则返回空列表）。"""
    notes = []

    with db.engine.begin() as connection:
        tables = _table_names(connection)
        if not tables & {"user", "activity", "enrollment"}:
            return notes  # 空库：没有需要迁移的东西

        # ---- 1. 补新列 ----
        for table, columns in NEW_COLUMNS.items():
            if table not in tables:
                continue
            existing = _columns(connection, table)
            for name, ddl in columns:
                if name not in existing:
                    connection.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    )
                    notes.append(f"新增列 {table}.{name}")

        # ---- 2. 翻译旧取值 ----
        for (table, column), pairs in VALUE_MAP.items():
            if table not in tables:
                continue
            for old, new in pairs:
                result = connection.execute(
                    text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old"),
                    {"new": new, "old": old},
                )
                if result.rowcount:
                    notes.append(
                        f"{table}.{column}: {old} → {new}（{result.rowcount} 行）"
                    )

    return notes
