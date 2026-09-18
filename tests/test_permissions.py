"""权限控制测试。

设计文档第 2.2 节的硬性要求：**每个写接口都必须在服务端校验角色与归属权**。
这一组用例就是用来证明「前端藏按钮」之外，服务端确实拦得住。

另外还要证明一件事：**角色只能来自服务端**。任何"客户端传什么就用什么"
的写法，都等于把权限系统交给攻击者。
"""

from __future__ import annotations

import pytest

from app.models import Role

from conftest import DEFAULT_PASSWORD


# ---------------------------------------------------------------------------
# 未登录
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/dashboard", "/admin/", "/club/"])
def test_anonymous_is_sent_to_login(client, path):
    """未登录访问受保护页面 → 跳登录页，并记住原本要去的地方。"""
    resp = client.get(path)
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "/auth/login" in location
    assert f"next={path}" in location


def test_anonymous_api_request_gets_json_not_html(client):
    """接口请求不能返回一整页 HTML —— 前端会解析失败并报出无关的错误。"""
    resp = client.get("/auth/me")
    assert resp.status_code == 401
    assert resp.is_json
    assert resp.get_json()["error"] == "未登录"


def test_anonymous_dashboard_with_json_accept_gets_json(client):
    resp = client.get("/dashboard", headers={"Accept": "application/json"})
    assert resp.status_code == 401
    assert resp.is_json


def test_anonymous_browser_request_still_gets_redirect(client):
    """浏览器发来的 Accept 里带 text/html，不能被当成接口请求。"""
    resp = client.get(
        "/dashboard",
        headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
    )
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# 已登录但角色不符
# ---------------------------------------------------------------------------


def test_admin_page_allows_admin(client, admin, login):
    login(admin.student_no)
    resp = client.get("/admin/")
    assert resp.status_code == 200
    assert "管理后台" in resp.get_data(as_text=True)


@pytest.mark.parametrize("role_fixture", ["student", "club"])
def test_admin_page_rejects_non_admin(client, login, request, role_fixture):
    user = request.getfixturevalue(role_fixture)
    login(user.student_no)
    resp = client.get("/admin/")
    assert resp.status_code == 403


def test_club_page_allows_club(client, club, login):
    login(club.student_no)
    resp = client.get("/club/")
    assert resp.status_code == 200
    assert "社团工作台" in resp.get_data(as_text=True)


def test_club_page_rejects_student(client, student, login):
    login(student.student_no)
    assert client.get("/club/").status_code == 403


def test_club_page_rejects_admin(client, admin, login):
    """管理员能管全部活动，但不等于能进社团工作台。

    这是刻意的：`role_required(Role.CLUB)` 只放行社团。管理员需要看社团的东西，
    应该走管理后台的入口，而不是借用社团身份——否则权限边界会变得模糊。
    """
    login(admin.student_no)
    assert client.get("/club/").status_code == 403


@pytest.mark.parametrize("role_fixture", ["student", "club", "admin"])
def test_dashboard_is_open_to_every_logged_in_role(client, login, request, role_fixture):
    user = request.getfixturevalue(role_fixture)
    login(user.student_no)
    assert client.get("/dashboard").status_code == 200


# ---------------------------------------------------------------------------
# 角色只能由服务端决定
# ---------------------------------------------------------------------------


def test_role_cannot_be_set_from_the_login_form(client, student, csrf_token):
    """登录时在表单里塞 role=ADMIN，不能真的变成管理员。

    这是「转换身份」这件事的关键：角色的唯一来源是数据库里的 user.role，
    跟请求里带了什么字段完全无关。
    """
    resp = client.post(
        "/auth/login",
        data={
            "csrf_token": csrf_token(),
            "student_no": student.student_no,
            "password": DEFAULT_PASSWORD,
            "role": "ADMIN",
            "is_admin": "1",
            "status": "1",
        },
    )
    assert resp.status_code == 302

    me = client.get("/auth/me").get_json()
    assert me["role"] == Role.STUDENT

    # 关键：就算前面都装得像管理员，这一步必须被拦下
    assert client.get("/admin/").status_code == 403


def test_me_returns_role_from_database(client, admin, login):
    login(admin.student_no)
    resp = client.get("/auth/me")
    assert resp.status_code == 200
    assert resp.get_json()["role"] == Role.ADMIN


# ---------------------------------------------------------------------------
# 会话 Cookie 被篡改
# ---------------------------------------------------------------------------


def test_tampered_session_cookie_is_rejected(app, student, login, client):
    """改一个字符，签名就对不上，服务端必须当作未登录。

    session Cookie 是用 SECRET_KEY 签名的。如果 SECRET_KEY 泄漏（比如代码里
    写死的默认值），攻击者就能自己签一个"我是管理员"的 Cookie——所以
    生产环境缺 SECRET_KEY 时宁可拒绝启动。
    """
    login(student.student_no)
    original = client.get_cookie("session").value
    tampered = original[:-3] + ("aaa" if not original.endswith("aaa") else "bbb")

    # 用一个全新的客户端，只带上被篡改的那个 Cookie
    fresh = app.test_client()
    resp = fresh.get("/dashboard", headers={"Cookie": f"session={tampered}"})

    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]
