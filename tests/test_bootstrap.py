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
