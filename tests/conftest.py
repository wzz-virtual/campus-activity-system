"""测试用的公共装置。

三条约定：

1. **每个测试一个干净的内存数据库**（``app`` fixture 是函数级的），
   测试之间不共享数据，也不依赖执行顺序。

2. **测试走真实链路，不关掉安全防护**。CSRF 在测试环境里是开着的，
   需要提交表单时由 ``csrf_token`` 装置去页面里把 token 取出来，
   而不是把 ``WTF_CSRF_ENABLED`` 设成 False。把防护关掉再测，
   测出来的只是"没有防护时系统能跑"，毫无意义。

3. ⚠️ **绝不能用 ``with app.app_context()`` 把整个测试包起来。**

   这一条踩过坑，写在这里避免以后再犯：Flask 的 ``g`` 是挂在**应用上下文**
   上的，而 ``RequestContext.push()`` 发现已有同 app 的应用上下文时会**直接复用**。
   一旦测试外层长期持有一个应用上下文，测试客户端的多次请求就共享同一个 ``g``，
   请求级的缓存会串味 —— 典型表现是 Flask-WTF 的 ``generate_csrf()`` 命中
   ``g.csrf_token`` 缓存后不再写入 session，于是第二个请求起 CSRF 必然失败。

   所以：应用上下文只在**需要直接读写数据库的那一小段**里开，
   用 ``db_session`` 装置。
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import create_app
from app.extensions import db as _db
from app.models import (
    Activity,
    ActivityStatus,
    Category,
    Enrollment,
    EnrollStatus,
    Role,
    User,
    utcnow,
)

# 测试统一用这个密码。长度满足密码策略（至少 8 位）
DEFAULT_PASSWORD = "Test-Password-2026"

# 匹配模板里隐藏的 csrf_token 字段
_CSRF_RE = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')


@pytest.fixture
def app():
    """每个测试一个全新的应用 + 空的内存数据库。

    建表和删表各用一次短命的应用上下文，测试主体期间不持有上下文，
    这样每个请求都会自己压入干净的上下文（见模块开头的第 3 条）。
    """
    application = create_app("test")

    with application.app_context():
        _db.create_all()

    yield application

    with application.app_context():
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db_session(app):
    """在应用上下文里开一个数据库会话，供测试直接读写数据。

    用法（注意必须有 with，不要用它包住整个测试）::

        with db_session() as session:
            user = session.get(User, student_id)
            user.locked_until = utcnow() - timedelta(seconds=1)
            session.commit()
    """

    @contextmanager
    def _open():
        with app.app_context():
            yield _db.session

    return _open


@pytest.fixture
def user_factory(app):
    """创建用户的工厂，返回一个轻量的只读句柄（不是 ORM 对象）。

    返回 ``SimpleNamespace`` 而不是 ``User`` 实例，是因为测试主体期间没有
    应用上下文，ORM 对象已经脱离会话；直接返回对象会诱使调用方去改它的属性，
    然后在别处莫名其妙地不生效。要改数据请走 ``db_session``。
    """

    def _make(
        student_no,
        name="测试用户",
        role=Role.STUDENT,
        password=DEFAULT_PASSWORD,
        active=True,
    ):
        with app.app_context():
            user = User(
                student_no=student_no,
                name=name,
                role=role,
                status=1 if active else 0,
            )
            user.set_password(password)
            _db.session.add(user)
            _db.session.commit()
            return SimpleNamespace(
                id=user.id,
                student_no=user.student_no,
                name=user.name,
                role=user.role,
            )

    return _make


@pytest.fixture
def student(user_factory):
    return user_factory("20230101", "李明", Role.STUDENT)


@pytest.fixture
def club(user_factory):
    return user_factory("club001", "摄影协会", Role.CLUB)


@pytest.fixture
def admin(user_factory):
    return user_factory("admin", "陈老师", Role.ADMIN)


@pytest.fixture
def csrf_token(client):
    """从页面里把 CSRF token 取出来。

    取不到就直接让测试失败——这说明表单漏了防护，属于必须修的问题，
    而不是"测试写不下去"。
    """

    def _get(path="/auth/login"):
        resp = client.get(path)
        match = _CSRF_RE.search(resp.get_data(as_text=True))
        assert match, f"{path} 的页面里没有 csrf_token 字段，表单缺少 CSRF 防护"
        return match.group(1)

    return _get


@pytest.fixture
def login(client, csrf_token):
    """按真实流程登录：先取 token，再连同表单一起提交。

    同一个测试里切换身份（社团 → 管理员）时能直接再用，因为这里会先把上一个
    身份登出。不这么做的话，``GET /auth/login`` 对已登录用户是 302 跳转，
    页面里根本没有表单，取 token 就会失败。
    """

    def _login(student_no, password=DEFAULT_PASSWORD, follow_redirects=False, **extra):
        if client.get("/dashboard").status_code == 200:
            client.post("/auth/logout", data={"csrf_token": csrf_token("/dashboard")})

        data = {
            "csrf_token": csrf_token(),
            "student_no": student_no,
            "password": password,
        }
        data.update(extra)
        return client.post("/auth/login", data=data, follow_redirects=follow_redirects)

    return _login


# ---------------------------------------------------------------------------
# 活动相关的装置
# ---------------------------------------------------------------------------


@pytest.fixture
def category(app):
    """一个现成的分类，测试里可以重复使用。"""
    with app.app_context():
        existing = _db.session.execute(
            _db.select(Category).where(Category.name == "讲座")
        ).scalar_one_or_none()
        if existing is None:
            existing = Category(name="讲座", sort_order=0)
            _db.session.add(existing)
            _db.session.commit()
        return SimpleNamespace(id=existing.id, name=existing.name)


@pytest.fixture
def activity_factory(app, category):
    """活动工厂。

    时间参数统一用「距现在多少小时」表示，这样时间边界的测试可以不写
    ``datetime`` 运算，一眼就能看出「截止还差 1 秒」还是「已经过了 1 秒」。
    需要精确到秒时，直接传 ``start_time`` / ``enroll_deadline`` 覆盖即可。
    """

    def _make(
        organizer_id,
        title="测试活动",
        category_id=None,
        status=ActivityStatus.PUBLISHED,
        start_in_hours=48,
        end_in_hours=50,
        deadline_in_hours=24,
        capacity=10,
        enrolled_count=0,
        start_time=None,
        end_time=None,
        enroll_deadline=None,
        **extra,
    ):
        with app.app_context():
            now = utcnow()
            activity = Activity(
                title=title,
                category_id=category_id if category_id is not None else category.id,
                organizer_id=organizer_id,
                start_time=start_time or (now + timedelta(hours=start_in_hours)),
                end_time=end_time or (now + timedelta(hours=end_in_hours)),
                enroll_deadline=enroll_deadline
                or (now + timedelta(hours=deadline_in_hours)),
                location=extra.pop("location", "测试地点"),
                capacity=capacity,
                enrolled_count=enrolled_count,
                status=status,
                **extra,
            )
            _db.session.add(activity)
            _db.session.commit()
            return SimpleNamespace(
                id=activity.id,
                title=activity.title,
                capacity=capacity,
                organizer_id=organizer_id,
            )

    return _make


@pytest.fixture
def activity_state(app):
    """读取活动当前落库的状态。

    返回 ``SimpleNamespace`` 而不是 ORM 对象：测试主体期间没有应用上下文，
    ORM 对象已经脱离会话，拿着它读属性会拿到过期值，还会诱使调用方直接改它。
    """

    def _get(activity_id):
        with app.app_context():
            activity = _db.session.get(Activity, activity_id)
            if activity is None:
                return None
            return SimpleNamespace(
                id=activity.id,
                title=activity.title,
                status=activity.status,
                capacity=activity.capacity,
                enrolled_count=activity.enrolled_count,
                check_in_code=activity.check_in_code,
                reject_reason=activity.reject_reason,
                approved_by=activity.approved_by,
            )

    return _get


@pytest.fixture
def mutate(app):
    """在应用上下文里直接改数据，用于铺陈测试的前置状态。

    用法::

        mutate(lambda s: setattr(s.get(Activity, aid), "status", "ONGOING"))
    """

    def _run(fn):
        with app.app_context():
            result = fn(_db.session)
            _db.session.commit()
            return result

    return _run


@pytest.fixture
def enroll_factory(app, activity_factory):
    """直接造报名记录，用于铺陈名单 / 统计的测试数据。

    注意它会**同步推进**活动的 ``enrolled_count``，因为这个字段是并发控制
    的载体，单独改报名表而不动计数，会造出生产里不可能出现的脏数据。
    """

    def _make(activity_id, user_ids, checked_in=(), status=EnrollStatus.ENROLLED):
        checked = set(checked_in)
        with app.app_context():
            activity = _db.session.get(Activity, activity_id)
            for index, user_id in enumerate(user_ids):
                is_checked = user_id in checked
                _db.session.add(
                    Enrollment(
                        activity_id=activity_id,
                        user_id=user_id,
                        status=status,
                        enrolled_at=utcnow(),
                        check_in_at=utcnow() if is_checked else None,
                    )
                )
                if status == EnrollStatus.ENROLLED:
                    activity.enrolled_count += 1
            _db.session.commit()
            return activity.enrolled_count

    return _make


@pytest.fixture
def concurrent_app(tmp_path):
    """并发测试专用的应用：**文件型** SQLite。

    为什么不能用内存库：SQLite 的内存库是「每个连接一份独立数据」，
    多线程测试里线程 A 写入的数据线程 B 根本看不见，各写各的——
    测出来的「没有超卖」是彻头彻尾的假象。

    ``check_same_thread=False`` 让连接可以跨线程使用；``timeout`` 给
    SQLite 的文件锁留出等待时间，否则并发写会直接抛 ``database is locked``。

    连接池要开得足够大：默认池是 5 + 10 溢出，而并发测试会同时开几十个线程。
    池子不够时线程会卡在「等一个可用连接」上，测出来的不是写入竞争，
    而是排队——既慢又测不到想测的东西。
    """
    database_path = tmp_path / "concurrent.db"
    application = create_app(
        "test",
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path.as_posix()}",
        SQLALCHEMY_ENGINE_OPTIONS={
            "connect_args": {"check_same_thread": False, "timeout": 30},
            "pool_size": 32,
            "max_overflow": 16,
        },
    )

    with application.app_context():
        _db.create_all()

    yield application

    with application.app_context():
        _db.session.remove()
        # 显式释放连接池。不 dispose 的话，文件句柄会一直挂在进程上，
        # 整套测试连跑几十个用例之后就可能把资源耗光。
        _db.engine.dispose()
        _db.drop_all()
