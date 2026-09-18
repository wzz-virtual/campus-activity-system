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
from types import SimpleNamespace

import pytest

from app import create_app
from app.extensions import db as _db
from app.models import Role, User

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
    """按真实流程登录：先取 token，再连同表单一起提交。"""

    def _login(student_no, password=DEFAULT_PASSWORD, follow_redirects=False, **extra):
        data = {
            "csrf_token": csrf_token(),
            "student_no": student_no,
            "password": password,
        }
        data.update(extra)
        return client.post("/auth/login", data=data, follow_redirects=follow_redirects)

    return _login
