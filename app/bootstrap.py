"""线上启动时的数据库准备。

线上沙箱里没有运维来跑 ``init-db`` / ``seed-demo`` / ``upgrade-v2``，
所以把这几步并进启动流程：**建表 → 迁移 → 播种**。

播种策略分两种：

* 默认（本地 / 通用）：**空库才播种**——库里已有数据就不动，避免把真实数据
  （用户刚建的活动、刚报的名）覆盖掉。
* ``force_reseed=True``（线上部署入口）：**每次启动都清空重播**一份新鲜演示数据。
  演示数据里的时间都是「相对播种那一刻」算的，放着不动会逐天过期（页面上只剩
  「已结束」「报名已截止」，看着像功能坏了）；每次启动重播，线上就永远是
  最新的、可完整体验的演示场景。代价是访问者在线上产生的改动会被清掉——
  演示站可接受，且线上只放演示数据、不放真实学生信息。
"""

from .extensions import db
from .mailbox import get_announcement, normalize_legacy_announcement
from .migrations import schema_needs_upgrade, upgrade_to_v2
from .models import User
from .seed import DEMO_ACCOUNTS, DEMO_PASSWORD, seed_demo_data


def database_is_empty():
    """库里一个用户都没有，视为空库。"""
    return not db.session.execute(
        db.select(db.func.count()).select_from(User)
    ).scalar_one()


def prepare_database(app, force_reseed=False):
    """建表；旧库先迁移到 V2.0；按策略播种。

    :param force_reseed: True 时**清空全部数据表再播种**（线上每次启动重播）；
         False（默认）时只在空库播种，已有数据不动。
    :return: 播种摘要，或 ``None``（库里已有数据、且非 force_reseed，未改动）
    """
    with app.app_context():
        if force_reseed:
            # 清空重播：drop_all 按依赖顺序删表，再 create_all 建出全新表，
            # 之后 database_is_empty() 必为真、直接进 seed_demo_data()。
            db.drop_all()
            db.create_all()
        else:
            db.create_all()

        # V1.0 的老库（线上沙箱里可能就躺着一个）：先把结构和取值迁移到
        # V2.0，再让应用跑起来。迁移是幂等的，每次启动查一下不花什么成本。
        # （force_reseed 走的是全新库，这一分支自然跳过。）
        if schema_needs_upgrade():
            notes = upgrade_to_v2()
            for note in notes:
                app.logger.info("V2.0 迁移：%s", note)

        # V2.0 的公告行：老库加完新表是空的，补一条（幂等）。
        # 公告默认不再预置文案（用户要求删掉首页说明文字）：
        # 仍存着旧默认文案的行在这里一并清空，教师自己写的不受影响。
        get_announcement()
        normalize_legacy_announcement()

        if not database_is_empty():
            return None
        summary = seed_demo_data()
        app.logger.info(
            "%s：%s 个分类 / %s 个账号 / %s 场活动（演示密码 %s）",
            "已重播演示数据" if force_reseed else "空库已初始化",
            summary["categories"], summary["users"], summary["activities"],
            DEMO_PASSWORD,
        )
        return summary


__all__ = ["prepare_database", "database_is_empty", "DEMO_ACCOUNTS", "DEMO_PASSWORD"]
