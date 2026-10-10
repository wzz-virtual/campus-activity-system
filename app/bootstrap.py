"""线上启动时的数据库准备。

线上沙箱里没有运维来跑 ``init-db`` / ``seed-demo``，所以把这两步并进启动流程。

**为什么是「空库才播种」而不是每次都重置**：无条件重置的话，线上沙箱重启一次
（或有人重新发布一次）就会把用户刚建的活动、刚报的名全部清掉——演示站看起来
就是「数据莫名消失」。只在空库时播种，既有开箱可用的演示数据，又不会中途把
别人的操作抹掉。
"""

from .extensions import db
from .models import User
from .seed import DEMO_ACCOUNTS, DEMO_PASSWORD, seed_demo_data


def database_is_empty():
    """库里一个用户都没有，视为空库。"""
    return not db.session.execute(
        db.select(db.func.count()).select_from(User)
    ).scalar_one()


def prepare_database(app):
    """建表；空库则铺一份演示数据。

    :return: 播种摘要，或 ``None``（库里已有数据，未改动）
    """
    with app.app_context():
        db.create_all()
        if not database_is_empty():
            return None
        summary = seed_demo_data()
        app.logger.info(
            "空库已初始化：%s 个分类 / %s 个账号 / %s 场活动（演示密码 %s）",
            summary["categories"], summary["users"], summary["activities"],
            DEMO_PASSWORD,
        )
        return summary


__all__ = ["prepare_database", "database_is_empty", "DEMO_ACCOUNTS", "DEMO_PASSWORD"]
