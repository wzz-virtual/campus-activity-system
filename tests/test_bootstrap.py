"""线上启动流程的测试（app/bootstrap.py 与 deploy.py）。

线上沙箱里没有运维，建表和铺演示数据都并进了启动流程。这块坏掉的表现是
「链接打得开但一片空白」或者「一进去 400」，在本地完全看不出来，所以单独立个文件。
"""

import os

import pytest

from app.bootstrap import database_is_empty, prepare_database
from app.extensions import db as _db
from app.models import Activity, Category, User
from app.seed import ACTIVITY_COUNT, DEMO_PASSWORD


def count(model):
    return _db.session.execute(_db.select(_db.func.count()).select_from(model)).scalar_one()


# ---------------------------------------------------------------------------
# 数据库准备
# ---------------------------------------------------------------------------


def test_prepare_database_seeds_an_empty_instance(app):
    """空库要能自己长出可用的演示数据。

    线上没人去跑 init-db / seed-demo，全靠启动时这一步。
    """
    with app.app_context():
        assert database_is_empty() is True

    summary = prepare_database(app)

    assert summary is not None
    assert summary["activities"] == ACTIVITY_COUNT

    with app.app_context():
        assert count(Category) == 4
        assert count(Activity) == ACTIVITY_COUNT
        assert count(User) == summary["users"]


def test_prepare_database_leaves_existing_data_alone(app, user_factory, mutate):
    """库里已经有数据时不能再播种。

    如果每次启动都无条件重置，线上沙箱重启一次（或有人重新发布一次）就会把
    用户刚建的活动、刚报的名全清掉——使用者只会看到「数据莫名消失」。
    """
    prepare_database(app)

    with app.app_context():
        someone_id = _db.session.execute(
            _db.select(User.id).where(User.student_no == "20230101")
        ).scalar_one()

    # 模拟「上线之后有人动过数据」：改个名字，再新建一个账号
    mutate(lambda session: setattr(session.get(User, someone_id), "name", "被改过的名字"))
    user_factory("20239999", "上线后注册的人", "STUDENT")

    second = prepare_database(app)

    assert second is None, "非空库不应该再播种"

    with app.app_context():
        assert count(Activity) == ACTIVITY_COUNT, "活动被重复播种了"
        assert count(User) == 18 + 1, "账号数不对，说明数据被重置过"
        assert _db.session.get(User, someone_id).name == "被改过的名字", "已有数据被覆盖了"
        assert _db.session.execute(
            _db.select(User).where(User.student_no == "20239999")
        ).scalar_one_or_none() is not None, "新加的账号被清掉了"


def test_prepare_database_creates_tables_first(app):
    """表还不存在时也要能跑通（真实场景就是「实例刚起、库文件还不存在」）。"""
    with app.app_context():
        _db.drop_all()

    summary = prepare_database(app)

    assert summary is not None
    with app.app_context():
        assert count(Activity) == ACTIVITY_COUNT


# ---------------------------------------------------------------------------
# V1.0 → V2.0 的就地迁移
# ---------------------------------------------------------------------------


def make_v1_database(application):
    """把一张库"降级"成 V1.0 的样子：去掉 V2.0 新列、写入旧取值。

    做法：先用当前模型建表，再把 V2.0 新增的列 ALTER 掉，
    然后插入旧词表（CLUB / PENDING / ENROLLED）的数据行。
    这样得到的就是一份货真价实的 V1.0 库——迁移代码面对的就是它。

    ⚠️ `reviewed_by` 留着不删：SQLite 不允许 DROP 被外键定义引用的列
    （`unknown column ... in foreign key definition`）。它对迁移没有影响——
    迁移是逐列检查"缺了才补"，列已经在就跳过，正好也顺带测了幂等性。
    """
    from sqlalchemy import text

    from app.extensions import db as _db

    with application.app_context():
        _db.create_all()
        for stmt in (
            "ALTER TABLE activity DROP COLUMN requires_review",
            "ALTER TABLE activity DROP COLUMN qualification",
            "ALTER TABLE enrollment DROP COLUMN waitlisted_at",
            "ALTER TABLE enrollment DROP COLUMN reviewed_at",
        ):
            _db.session.execute(text(stmt))

        # 三个角色 + 一个旧状态的活动 + 一条旧状态的报名
        _db.session.execute(
            text(
                "INSERT INTO user (student_no, name, password_hash, role, status,"
                " failed_login_count, must_change_password, created_at, updated_at)"
                " VALUES ('club001', '小张', 'x', 'CLUB', 1, 0, 0,"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        _db.session.execute(
            text(
                "INSERT INTO category (name, sort_order, created_at)"
                " VALUES ('讲座', 0, CURRENT_TIMESTAMP)"
            )
        )
        _db.session.execute(
            text(
                "INSERT INTO activity (title, category_id, organizer_id, start_time,"
                " end_time, capacity, enrolled_count, enroll_deadline, status,"
                " created_at, updated_at)"
                " VALUES ('旧活动', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 10, 1,"
                " CURRENT_TIMESTAMP, 'PENDING', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        _db.session.execute(
            text(
                "INSERT INTO enrollment (activity_id, user_id, status, enrolled_at)"
                " VALUES (1, 1, 'ENROLLED', CURRENT_TIMESTAMP)"
            )
        )
        _db.session.commit()


def test_prepare_database_migrates_a_v1_database_in_place(tmp_path):
    """V1.0 的老库要能就地升级到 V2.0——账号、活动、报名一条不少。

    线上沙箱里可能就躺着一个 V1.0 的库：直接换新代码跑，模型上的新列一张表
    也没有，第一个查询就会炸。迁移必须发生在任何业务查询之前。

    翻译规则：CLUB→TEACHER；PENDING→PUBLISHED（审核流取消，视为已发布）；
    ENROLLED→CONFIRMED（V2.0 里叫「正式」）。
    """
    from sqlalchemy import text

    from app import create_app
    from app.extensions import db as _db
    from app.migrations import schema_needs_upgrade

    database = tmp_path / "old.db"
    application = create_app(
        "test", SQLALCHEMY_DATABASE_URI=f"sqlite:///{database.as_posix()}"
    )
    make_v1_database(application)

    with application.app_context():
        assert schema_needs_upgrade() is True, "这份库没被识别成 V1.0"

    summary = prepare_database(application)

    # 库里有数据 → 不播种，但迁移必须已经完成
    assert summary is None

    with application.app_context():
        from app.models import Activity, Role, User

        assert schema_needs_upgrade() is False, "迁移之后结构应当已是 V2.0"

        user = _db.session.execute(
            _db.select(User).where(User.student_no == "club001")
        ).scalar_one()
        assert user.role == Role.TEACHER, "CLUB 应当被翻译成 TEACHER"

        activity = _db.session.execute(_db.select(Activity)).scalar_one()
        assert activity.status == "PUBLISHED", "PENDING 应当被翻译成 PUBLISHED"
        assert activity.requires_review is False, "新列应当有默认值"
        assert activity.qualification is None

        row = _db.session.execute(
            text("SELECT status FROM enrollment WHERE id = 1")
        ).one()
        assert row[0] == "CONFIRMED", "ENROLLED 应当被翻译成 CONFIRMED"

        # 新列由迁移补齐（逐列检查，缺一个补一个）
        activity_columns = {
            item[1]
            for item in _db.session.execute(text("PRAGMA table_info(activity)"))
        }
        assert {"requires_review", "qualification"} <= activity_columns
        enrollment_columns = {
            item[1]
            for item in _db.session.execute(text("PRAGMA table_info(enrollment)"))
        }
        assert {"waitlisted_at", "reviewed_at", "reviewed_by"} <= enrollment_columns


def test_upgrade_to_v2_is_idempotent(tmp_path):
    """迁移可以重复执行：已经是 V2.0 的库再跑一次，什么都不改。"""
    from app import create_app
    from app.extensions import db as _db
    from app.migrations import schema_needs_upgrade, upgrade_to_v2

    database = tmp_path / "fresh.db"
    application = create_app(
        "test", SQLALCHEMY_DATABASE_URI=f"sqlite:///{database.as_posix()}"
    )

    with application.app_context():
        _db.create_all()
        assert schema_needs_upgrade() is False
        assert upgrade_to_v2() == [], "新库不需要迁移，也不该报错"

    make_v1_database(application)

    with application.app_context():
        first = upgrade_to_v2()
        assert first, "V1.0 的库应当产生迁移动作"
        second = upgrade_to_v2()
        assert second == [], "重复迁移应当无操作"


# ---------------------------------------------------------------------------
# 启动入口的环境准备
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_deploy_env(monkeypatch):
    for key in ("FLASK_CONFIG", "SECRET_KEY", "DATABASE_URL", "SESSION_COOKIE_SECURE"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_deploy_uses_production_config_and_a_real_secret(clean_deploy_env):
    """生产配置 + 真随机密钥。

    ProdConfig 会拒绝「缺 SECRET_KEY」或「用开发占位值」的启动，这个自检是对的，
    所以启动入口补一个真密钥而不是绕过检查——线上开 DEBUG 等于把调试器
    （任意代码执行能力）放到公网上。
    """
    import deploy

    deploy.configure_environment()

    assert os.environ["FLASK_CONFIG"] == "prod"
    assert len(os.environ["SECRET_KEY"]) >= 32
    assert not os.environ["SECRET_KEY"].startswith(("dev-only", "test-only"))


def test_deploy_points_at_its_own_database_file(clean_deploy_env):
    """线上库必须是独立文件，不能和本地开发库共用。

    共用的话，线上会被本地那份「放了几天的旧数据」污染，本地调试也会误改线上数据。
    """
    import deploy

    deploy.configure_environment()
    url = os.environ["DATABASE_URL"].replace("\\", "/")

    assert "instance/live.db" in url
    assert "campus.db" not in url, "线上不能指向本地开发用的 campus.db"


def test_deploy_does_not_override_explicit_environment(clean_deploy_env):
    """已经显式设好的环境变量不能被覆盖。

    真实部署要求注入一个**固定**的 SECRET_KEY：随机密钥每次重启都变，
    意味着重启后所有人的登录会话一起失效。
    """
    import deploy

    clean_deploy_env.setenv("SECRET_KEY", "x" * 48)
    clean_deploy_env.setenv("DATABASE_URL", "sqlite:////data/prod.db")

    deploy.configure_environment()

    assert os.environ["SECRET_KEY"] == "x" * 48
    assert os.environ["DATABASE_URL"] == "sqlite:////data/prod.db"


def test_build_app_keeps_secure_cookies_on_by_default(clean_deploy_env, tmp_path):
    """默认要求 HTTPS 传输 session Cookie，但留了显式开关。

    Secure Cookie 在纯 HTTP 下不会被客户端回传，表现为「一提交登录就 400」
    （CSRF token 对不上）——这个现象和「密码错了」很像，很容易查偏。
    所以线上确认是按 HTTP 访问时，用 SESSION_COOKIE_SECURE=0 关掉。
    """
    import deploy

    # 必须把库指到临时目录：build_app 会真的建库并播种，
    # 不隔离的话测试会去动仓库里的 instance/live.db。
    clean_deploy_env.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'a.db').as_posix()}")

    app = deploy.build_app()
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["DEBUG"] is False

    clean_deploy_env.setenv("SESSION_COOKIE_SECURE", "0")
    app = deploy.build_app()
    assert app.config["SESSION_COOKIE_SECURE"] is False


def test_demo_password_is_documented_and_usable(clean_deploy_env, tmp_path):
    """演示密码是公开的——这条测试只是把它钉在明面上。

    线上是公开地址，任何人都能用这组账号登录（包括管理员）。所以站点里
    只能放演示数据，绝不能放真实的学生信息。
    """
    import deploy

    clean_deploy_env.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'b.db').as_posix()}")

    app = deploy.build_app()
    with app.app_context():
        admin = _db.session.execute(
            _db.select(User).where(User.student_no == "admin")
        ).scalar_one()
        assert admin.check_password(DEMO_PASSWORD)
