"""安全基线的测试。

覆盖三块：

1. **CSRF** —— 跨站伪造的 POST 必须被拦下，且拦截要发生在业务逻辑之前；
2. **会话与响应头** —— Cookie 标志、安全响应头、请求体大小限制；
3. **生产环境配置自检** —— 缺 SECRET_KEY 这类问题必须让应用拒绝启动，
   而不是带病上线。
"""

from __future__ import annotations

import pytest

from app import create_app
from app.config import MAX_CONTENT_LENGTH_BYTES, ProdConfig
from app.models import Role, User

from conftest import DEFAULT_PASSWORD

# 一个够长、能通过生产配置自检的密钥
VALID_PROD_KEY = "a" * 64


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def test_login_without_csrf_token_is_rejected(client, student):
    """不带 CSRF 凭证的登录 POST 必须被拒。

    没有这道防线，攻击者可以在自己的页面里放一个自动提交的表单，
    把访问者的浏览器悄悄登到攻击者指定的账号上，之后再诱导他输入敏感信息。
    """
    resp = client.post(
        "/auth/login",
        data={"student_no": student.student_no, "password": DEFAULT_PASSWORD},
    )
    assert resp.status_code == 400


def test_login_with_bogus_csrf_token_is_rejected(client, student):
    resp = client.post(
        "/auth/login",
        data={
            "csrf_token": "obviously-not-a-valid-token",
            "student_no": student.student_no,
            "password": DEFAULT_PASSWORD,
        },
    )
    assert resp.status_code == 400


def test_logout_without_csrf_token_does_not_log_out(client, student, login):
    """光拒绝还不够 —— 要确认业务真的没有执行。

    如果代码写成"先登出，再校验 CSRF"，状态码可能被改对，但攻击者已经
    用伪造请求把用户的登录态清掉了。
    """
    login(student.student_no)
    assert client.get("/dashboard").status_code == 200

    resp = client.post("/auth/logout")
    assert resp.status_code == 400

    # 关键断言：人还登着
    assert client.get("/dashboard").status_code == 200


def test_login_with_valid_csrf_token_succeeds(client, student, login):
    """反向验证：正常的 CSRF 流程不能被误伤。"""
    resp = login(student.student_no)
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# Cookie 与响应头
# ---------------------------------------------------------------------------


def test_session_cookie_is_hardened(client, student, login):
    """session Cookie 必须带 HttpOnly 与 SameSite。

    * HttpOnly —— 浏览器里的脚本读不到它，XSS 也偷不走登录态；
    * SameSite=Lax —— 跨站的 POST / iframe 不会带上它，挡住大部分 CSRF。
    """
    resp = login(student.student_no)
    cookies = [c for c in resp.headers.getlist("Set-Cookie") if c.startswith("session=")]
    assert cookies, "登录响应里没有下发 session Cookie"

    cookie = cookies[0].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_production_requires_secure_cookie_flag():
    """生产环境必须只走 HTTPS 传输 Cookie，否则同一网络下就能被嗅探到。"""
    assert ProdConfig.SESSION_COOKIE_SECURE is True
    assert ProdConfig.REMEMBER_COOKIE_SECURE is True


def test_security_headers_are_present(client):
    resp = client.get("/")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "same-origin"


def test_oversized_request_body_is_rejected(client, student, csrf_token):
    """超大请求体应被拒绝，而不是先把内存吃掉再说。"""
    oversized = "x" * (MAX_CONTENT_LENGTH_BYTES + 4096)
    resp = client.post(
        "/auth/login",
        data={
            "csrf_token": csrf_token(),
            "student_no": student.student_no,
            "password": oversized,
        },
    )
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# 密码策略
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("weak", ["", "123", "1234567", "short"])
def test_short_password_is_refused_at_the_model_layer(app, weak):
    """密码最小长度卡在模型层。

    放在 set_password 里，是为了让"写入密码"这个必经点成为唯一入口 ——
    否则将来加了改密码、管理员重置密码等入口，很容易漏掉校验。
    """
    user = User(student_no="weaktest", name="弱密码", role=Role.STUDENT)
    with pytest.raises(ValueError):
        user.set_password(weak)


def test_acceptable_password_is_hashed_not_stored_plainly(app):
    user = User(student_no="hashtest", name="正常用户", role=Role.STUDENT)
    user.set_password(DEFAULT_PASSWORD)

    assert DEFAULT_PASSWORD not in user.password_hash
    assert user.check_password(DEFAULT_PASSWORD)
    assert not user.check_password(DEFAULT_PASSWORD + "x")


# ---------------------------------------------------------------------------
# 生产环境配置自检
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_prod_env(monkeypatch):
    """把两个关键环境变量清掉，让每个用例从干净状态开始。"""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


def test_prod_refuses_to_start_without_secret_key(clean_prod_env, monkeypatch):
    """缺 SECRET_KEY 必须拒绝启动。

    这是最要命的一条：SECRET_KEY 是用来给 session Cookie 签名的。
    它一旦是公开的默认值，任何人都能自己签一个「我是管理员」的 Cookie ——
    后面所有的角色校验、归属权校验全部形同虚设。
    """
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://u:p@localhost/campus")

    with pytest.raises(RuntimeError) as exc:
        create_app("prod")

    assert "SECRET_KEY" in str(exc.value)


def test_prod_refuses_placeholder_secret_key(clean_prod_env, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "dev-only-insecure-key-please-set-SECRET_KEY")
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://u:p@localhost/campus")

    with pytest.raises(RuntimeError) as exc:
        create_app("prod")

    assert "占位值" in str(exc.value)


def test_prod_refuses_short_secret_key(clean_prod_env, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "tooshort")
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://u:p@localhost/campus")

    with pytest.raises(RuntimeError) as exc:
        create_app("prod")

    assert "太短" in str(exc.value)


def test_prod_refuses_to_start_without_database_url(clean_prod_env, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", VALID_PROD_KEY)

    with pytest.raises(RuntimeError) as exc:
        create_app("prod")

    assert "DATABASE_URL" in str(exc.value)


def test_prod_starts_with_proper_configuration(clean_prod_env, monkeypatch):
    """反向验证：配置齐全时必须能正常启动，否则上面那些测试只是在证明
    "它总是报错"而已。"""
    monkeypatch.setenv("SECRET_KEY", VALID_PROD_KEY)
    # 用一个不需要额外驱动的地址：这条用例验证的是配置自检，不是数据库连通性
    monkeypatch.setenv("DATABASE_URL", "sqlite://")

    app = create_app("prod")

    assert app.config["SECRET_KEY"] == VALID_PROD_KEY
    assert app.config["DEBUG"] is False
    assert app.config["SESSION_COOKIE_SECURE"] is True


def test_dev_environment_does_not_require_secret_key(clean_prod_env, monkeypatch):
    """开发环境不该被这些检查卡住，否则本地根本没法跑。"""
    app = create_app("dev", SQLALCHEMY_DATABASE_URI="sqlite://")
    assert app.config["SECRET_KEY"]

    app2 = create_app("dev", SECRET_KEY="anything", SQLALCHEMY_DATABASE_URI="sqlite://")
    assert app2.config["SECRET_KEY"] == "anything"
