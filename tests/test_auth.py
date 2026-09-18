"""登录环节的安全测试。

这一层是整个系统最关键的防线：登录一旦被绕过，后面所有的角色校验、
归属权校验都没有意义。所以下面按「攻击者会怎么做」来组织用例。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.auth import LOGIN_FAILED_MESSAGE
from app.config import LOGIN_MAX_ATTEMPTS
from app.models import User, utcnow

from conftest import DEFAULT_PASSWORD


# ---------------------------------------------------------------------------
# 正常流程
# ---------------------------------------------------------------------------


def test_login_page_is_reachable(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert "学号 / 工号" in resp.get_data(as_text=True)


def test_login_succeeds_with_correct_password(client, student, login):
    resp = login(student.student_no)
    assert resp.status_code == 302
    assert "/dashboard" in resp.headers["Location"]


def test_logged_in_user_is_redirected_away_from_login_page(client, student, login):
    login(student.student_no)
    resp = client.get("/auth/login")
    assert resp.status_code == 302
    assert "/dashboard" in resp.headers["Location"]


def test_logout_clears_session(client, student, login, csrf_token):
    login(student.student_no)
    token_after_login = csrf_token("/dashboard")

    resp = client.post("/auth/logout", data={"csrf_token": token_after_login})
    assert resp.status_code == 302

    with client.session_transaction() as sess:
        assert "_user_id" not in sess


# ---------------------------------------------------------------------------
# 账号枚举：不能让攻击者问出"哪些学号是真实存在的"
# ---------------------------------------------------------------------------


def test_wrong_password_and_unknown_account_give_identical_response(
    client, student, csrf_token
):
    """密码错 和 账号不存在，返回的状态码与提示语必须完全一样。

    只要有一处不同，攻击者就能拿一份学号名单批量探测，筛出有效账号，
    再对这些账号集中撞库——工作量从"全体学生"缩小到"筛出来的那批"。
    """
    token = csrf_token()
    wrong_password = client.post(
        "/auth/login",
        data={
            "csrf_token": token,
            "student_no": student.student_no,
            "password": "definitely-wrong-password",
        },
    )
    unknown_account = client.post(
        "/auth/login",
        data={
            "csrf_token": token,
            "student_no": "99999999",
            "password": "definitely-wrong-password",
        },
    )

    assert wrong_password.status_code == unknown_account.status_code == 401
    assert LOGIN_FAILED_MESSAGE in wrong_password.get_data(as_text=True)
    assert LOGIN_FAILED_MESSAGE in unknown_account.get_data(as_text=True)


def test_unknown_account_still_runs_a_password_hash(client, csrf_token, monkeypatch):
    """账号不存在时也必须走一次哈希计算。

    否则「查不到用户 → 立刻返回」和「查到用户 → 算一次哈希再返回」之间
    会有几十毫秒的差距，攻击者逐条计时就能把有效学号筛出来。
    这是一个纯时序侧信道，抓包看不出来，但确实能被利用。
    """
    from app import auth

    calls = []
    real_hash = auth.check_password_hash

    def spy(password_hash, password):
        calls.append(password_hash)
        return real_hash(password_hash, password)

    monkeypatch.setattr(auth, "check_password_hash", spy)

    resp = client.post(
        "/auth/login",
        data={
            "csrf_token": csrf_token(),
            "student_no": "not-a-real-account",
            "password": "whatever",
        },
    )

    assert resp.status_code == 401
    assert len(calls) == 1, "账号不存在时跳过了哈希计算，存在时序侧信道"


def test_disabled_account_cannot_login(client, user_factory, csrf_token, login):
    """密码正确但账号被停用 → 拒绝，且状态码与密码错误区分开。"""
    user_factory("20230109", "已停用用户", active=False)

    resp = login("20230109")
    assert resp.status_code == 403
    assert "停用" in resp.get_data(as_text=True)


def test_disabled_status_is_not_revealed_without_correct_password(
    client, user_factory, csrf_token, login
):
    """没通过密码验证时，不能暴露"这个账号存在但被停用了"。"""
    user_factory("20230110", "已停用用户", active=False)

    resp = login("20230110", password="wrong-password")
    assert resp.status_code == 401
    assert "停用" not in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# 撞库防护：连续失败就临时锁定
# ---------------------------------------------------------------------------


def test_account_locks_after_repeated_failures(client, student, csrf_token):
    token = csrf_token()
    for _ in range(LOGIN_MAX_ATTEMPTS):
        resp = client.post(
            "/auth/login",
            data={
                "csrf_token": token,
                "student_no": student.student_no,
                "password": "wrong-password",
            },
        )
        assert resp.status_code == 401

    # 第 6 次即使密码正确也进不去
    resp = client.post(
        "/auth/login",
        data={
            "csrf_token": token,
            "student_no": student.student_no,
            "password": DEFAULT_PASSWORD,
        },
    )
    assert resp.status_code == 429
    assert "锁定" in resp.get_data(as_text=True)


def test_lock_expires_automatically(client, student, db_session, login):
    """锁定到点自动放行，不需要管理员手工解锁。"""
    with db_session() as session:
        user = session.get(User, student.id)
        user.locked_until = utcnow() - timedelta(seconds=1)
        user.failed_login_count = LOGIN_MAX_ATTEMPTS
        session.commit()

    resp = login(student.student_no)
    assert resp.status_code == 302


def test_successful_login_resets_failure_counter(client, student, db_session, login):
    login(student.student_no, password="wrong-password")
    with db_session() as session:
        assert session.get(User, student.id).failed_login_count == 1

    resp = login(student.student_no)
    assert resp.status_code == 302

    with db_session() as session:
        user = session.get(User, student.id)
        assert user.failed_login_count == 0
        assert user.locked_until is None


# ---------------------------------------------------------------------------
# 开放重定向：登录后的跳转目标不能任由攻击者指定
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile_next",
    [
        "//evil.com",
        "/\\evil.com",
        "\\\\evil.com",
        "https://evil.com",
        "http://evil.com/path",
        "javascript:alert(1)",
        "/dashboard\n//evil.com",
        "",
    ],
)
def test_open_redirect_is_blocked(client, admin, csrf_token, hostile_next):
    """各种绕过写法都必须退回默认页面，绝不能跳到站外。

    ``/\\evil.com`` 这条尤其容易漏：不少浏览器的 URL 解析器会把反斜杠
    当成正斜杠，只判断 ``startswith("/") and not startswith("//")`` 是挡不住的。
    """
    resp = client.post(
        "/auth/login",
        data={
            "csrf_token": csrf_token(),
            "student_no": admin.student_no,
            "password": DEFAULT_PASSWORD,
            "next": hostile_next,
        },
    )
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "evil.com" not in location
    assert location.endswith("/dashboard")


def test_safe_next_is_honoured(client, admin, csrf_token):
    resp = client.post(
        "/auth/login",
        data={
            "csrf_token": csrf_token(),
            "student_no": admin.student_no,
            "password": DEFAULT_PASSWORD,
            "next": "/admin/",
        },
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/admin/")


# ---------------------------------------------------------------------------
# 会话固定（session fixation）
# ---------------------------------------------------------------------------


def test_login_rebuilds_the_session(client, student, csrf_token):
    """登录必须换一个全新的会话。

    攻击者如果能事先把自己的 session Cookie 塞进受害者浏览器（比如通过
    一个能写 Cookie 的子域），登录后双方就共用同一个会话。登录时先清空、
    再写入登录态，可以断掉这条路。
    """
    token = csrf_token()
    with client.session_transaction() as sess:
        sess["planted_before_login"] = "attacker-value"

    resp = client.post(
        "/auth/login",
        data={
            "csrf_token": token,
            "student_no": student.student_no,
            "password": DEFAULT_PASSWORD,
        },
    )
    assert resp.status_code == 302

    with client.session_transaction() as sess:
        assert "planted_before_login" not in sess, "登录没有重建会话，存在会话固定风险"
        assert sess.get("_user_id") is not None


def test_logout_wipes_session_even_for_anonymous_request(client, student, login, csrf_token):
    """登出后会话里不应残留任何登录态。"""
    login(student.student_no)
    token = csrf_token("/dashboard")
    client.post("/auth/logout", data={"csrf_token": token})

    with client.session_transaction() as sess:
        assert "_user_id" not in sess
        assert "_fresh" not in sess
