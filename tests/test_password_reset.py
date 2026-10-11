"""「忘记密码」找回流程的测试。

采用方案 B：学生提交申请 → 管理员核实身份 → 系统重置为随机临时密码 →
学生登录后被强制修改密码。

这一组用例重点盯三件事：

1. **不能变成账号枚举工具** —— 找不回密码的页面比登录页更危险，
   因为登录页有失败次数限制，这里没有。提交不存在的学号必须得到相同反馈。
2. **管理员不能知道用户的最终密码** —— 只发临时密码，且强制本人改掉。
3. **强制改密必须拦在每个请求上** —— 只在登录那一步跳转，
   用户敲地址栏就能绕过去。
"""

from __future__ import annotations

import re

import pytest

from app.extensions import db
from app.models import PasswordResetRequest, ResetRequestStatus, Role, User

from conftest import DEFAULT_PASSWORD

CSRF_RE = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')
TEMP_PASSWORD_RE = re.compile(r'<code class="fs-5 user-select-all">([^<]+)</code>')

NEW_PASSWORD = "Brand-New-Pass-2026"


def _token(client, path):
    """从页面里取 CSRF token。要求页面必须真的渲染出表单。"""
    resp = client.get(path)
    match = CSRF_RE.search(resp.get_data(as_text=True))
    assert match, f"{path} 页面里没有 csrf_token"
    return match.group(1)


def _login(client, student_no, password=DEFAULT_PASSWORD):
    return client.post(
        "/auth/login",
        data={
            "csrf_token": _token(client, "/auth/login"),
            "student_no": student_no,
            "password": password,
        },
    )


def _submit_reset(client, student_no, note=""):
    return client.post(
        "/auth/forgot-password",
        data={
            "csrf_token": _token(client, "/auth/forgot-password"),
            "student_no": student_no,
            "note": note,
        },
    )


@pytest.fixture
def reset_request(client, student, db_session):
    """先造一条待处理的找回申请，返回它的 id。"""
    _submit_reset(client, student.student_no)
    with db_session() as session:
        return session.execute(db.select(PasswordResetRequest.id)).scalar_one()


# ---------------------------------------------------------------------------
# 申请环节
# ---------------------------------------------------------------------------


def test_forgot_password_page_is_reachable_without_login(client):
    resp = client.get("/auth/forgot-password")
    assert resp.status_code == 200
    assert "忘记密码" in resp.get_data(as_text=True)


def test_login_page_links_to_forgot_password(client):
    assert "/auth/forgot-password" in client.get("/auth/login").get_data(as_text=True)


def test_submitting_a_request_creates_pending_record(client, student, db_session):
    resp = _submit_reset(client, student.student_no, note="2023级 计算机 138xxxx1234")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]

    with db_session() as session:
        rows = session.execute(db.select(PasswordResetRequest)).scalars().all()
        assert len(rows) == 1
        assert rows[0].user_id == student.id
        assert rows[0].status == ResetRequestStatus.PENDING
        assert "138xxxx1234" in rows[0].note


def test_unknown_student_number_is_not_distinguishable(client, student, db_session):
    """不存在的学号必须给出与存在的学号**完全相同**的反馈。

    否则这个页面就成了账号枚举工具，而且比登录页更好用——
    登录页连续失败 5 次会被锁，这里没有限制。
    """
    existing = _submit_reset(client, student.student_no)
    unknown = _submit_reset(client, "99999999")

    assert existing.status_code == unknown.status_code
    assert existing.headers["Location"] == unknown.headers["Location"]

    # 而且不能给不存在的学号建记录
    with db_session() as session:
        assert session.execute(
            db.select(db.func.count()).select_from(PasswordResetRequest)
        ).scalar_one() == 1


def test_repeated_submissions_do_not_pile_up(client, student, db_session):
    """同一个人重复提交只保留一条待处理，避免刷满管理员的队列。"""
    for _ in range(3):
        _submit_reset(client, student.student_no)

    with db_session() as session:
        assert session.execute(
            db.select(db.func.count()).select_from(PasswordResetRequest)
        ).scalar_one() == 1


def test_forgot_password_requires_csrf_token(client, student, db_session):
    resp = client.post("/auth/forgot-password", data={"student_no": student.student_no})
    assert resp.status_code == 400
    with db_session() as session:
        assert session.execute(
            db.select(db.func.count()).select_from(PasswordResetRequest)
        ).scalar_one() == 0


# ---------------------------------------------------------------------------
# 处理队列的权限
# ---------------------------------------------------------------------------


def test_queue_requires_login(client):
    resp = client.get("/admin/password-resets")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


@pytest.mark.parametrize("role_fixture", ["student", "teacher"])
def test_queue_rejects_non_admin(client, login, request, role_fixture):
    user = request.getfixturevalue(role_fixture)
    login(user.student_no)
    assert client.get("/admin/password-resets").status_code == 403


def test_queue_allows_admin(client, admin, login):
    login(admin.student_no)
    resp = client.get("/admin/password-resets")
    assert resp.status_code == 200
    assert "密码找回申请" in resp.get_data(as_text=True)


def test_resolve_requires_admin(client, student, reset_request):
    login_resp = _login(client, student.student_no)
    assert login_resp.status_code == 302
    resp = client.post(
        f"/admin/password-resets/{reset_request}/resolve",
        data={"csrf_token": _token(client, "/dashboard")},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 重置环节
# ---------------------------------------------------------------------------


def test_admin_reset_issues_temporary_password(app, client, student, admin, db_session, reset_request):
    _login(client, admin.student_no)

    token = _token(client, "/admin/password-resets")
    resp = client.post(
        f"/admin/password-resets/{reset_request}/resolve",
        data={"csrf_token": token},
    )
    assert resp.status_code == 200

    match = TEMP_PASSWORD_RE.search(resp.get_data(as_text=True))
    assert match, "重置后没有在页面上显示临时密码"
    temp_password = match.group(1)
    assert len(temp_password) == 12

    with db_session() as session:
        record = session.get(PasswordResetRequest, reset_request)
        assert record.status == ResetRequestStatus.RESOLVED
        assert record.handled_by == admin.id
        assert record.handled_at is not None

        target = session.get(User, student.id)
        # 临时密码已生效，且标记为必须改密
        assert target.check_password(temp_password)
        assert target.must_change_password is True
        # 管理员始终看不到最终密码——用户还没改，所以旧密码必须失效
        assert not target.check_password(DEFAULT_PASSWORD)


def test_temporary_password_is_not_shown_twice(app, client, admin, student, db_session, reset_request):
    """临时密码只在生成时显示一次。刷新或重放这个请求都不该再看到。"""
    _login(client, admin.student_no)
    token = _token(client, "/admin/password-resets")
    first = client.post(
        f"/admin/password-resets/{reset_request}/resolve", data={"csrf_token": token}
    )
    assert TEMP_PASSWORD_RE.search(first.get_data(as_text=True))

    second = client.post(
        f"/admin/password-resets/{reset_request}/resolve", data={"csrf_token": token}
    )
    # 已处理过，被挡回列表页，且不再显示任何密码
    assert second.status_code == 302
    assert not TEMP_PASSWORD_RE.search(second.get_data(as_text=True))

    with db_session() as session:
        record = session.get(PasswordResetRequest, reset_request)
        target = session.get(User, student.id)
        # 第二次请求没有再改密码
        assert record.status == ResetRequestStatus.RESOLVED
        assert target.must_change_password is True


def test_admin_can_reject_request(client, admin, student, db_session, reset_request):
    _login(client, admin.student_no)
    token = _token(client, "/admin/password-resets")
    resp = client.post(
        f"/admin/password-resets/{reset_request}/reject",
        data={"csrf_token": token, "note": "无法核实身份"},
    )
    assert resp.status_code == 302

    with db_session() as session:
        record = session.get(PasswordResetRequest, reset_request)
        assert record.status == ResetRequestStatus.REJECTED
        assert record.handle_note == "无法核实身份"
        # 关键：没有动用户的密码
        assert session.get(User, student.id).check_password(DEFAULT_PASSWORD)


# ---------------------------------------------------------------------------
# 强制改密
# ---------------------------------------------------------------------------


def test_reset_triggers_forced_password_change(app, client, student, admin, db_session, reset_request):
    """完整走一遍：重置 → 临时密码登录 → 被拦住 → 改密 → 恢复正常。"""
    _login(client, admin.student_no)
    token = _token(client, "/admin/password-resets")
    resp = client.post(
        f"/admin/password-resets/{reset_request}/resolve", data={"csrf_token": token}
    )
    temp_password = TEMP_PASSWORD_RE.search(resp.get_data(as_text=True)).group(1)

    # 学生用临时密码登录（换一个客户端，免得混着管理员的会话）
    student_client = app.test_client()
    login_resp = _login(student_client, student.student_no, temp_password)
    assert login_resp.status_code == 302
    assert "/auth/change-password" in login_resp.headers["Location"]

    # 改密之前，敲任何地址都会被拦回改密页
    for path in ("/dashboard", "/teacher/", "/admin/", "/auth/me"):
        blocked = student_client.get(path)
        assert blocked.status_code == 302, f"{path} 没有被拦住"
        assert "/auth/change-password" in blocked.headers["Location"]

    # 改密
    changed = student_client.post(
        "/auth/change-password",
        data={
            "csrf_token": _token(student_client, "/auth/change-password"),
            "current_password": temp_password,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
    )
    assert changed.status_code == 302

    # 改完之后一切恢复正常，临时密码立即失效
    assert student_client.get("/dashboard").status_code == 200

    with db_session() as session:
        target = session.get(User, student.id)
        assert target.must_change_password is False
        assert target.check_password(NEW_PASSWORD)
        assert not target.check_password(temp_password)


@pytest.mark.parametrize(
    "payload, expected_message",
    [
        ({"current_password": "wrong-one"}, "当前密码不正确"),
        ({"new_password": "short", "confirm_password": "short"}, "不能少于"),
        ({"confirm_password": "Mismatch-Pass-2026"}, "不一致"),
        ({"new_password": DEFAULT_PASSWORD, "confirm_password": DEFAULT_PASSWORD}, "不能与当前密码相同"),
    ],
)
def test_change_password_validates(client, student, login, payload, expected_message):
    login(student.student_no)
    data = {
        "csrf_token": _token(client, "/auth/change-password"),
        "current_password": DEFAULT_PASSWORD,
        "new_password": NEW_PASSWORD,
        "confirm_password": NEW_PASSWORD,
    }
    data.update(payload)

    resp = client.post("/auth/change-password", data=data)
    assert resp.status_code == 400
    assert expected_message in resp.get_data(as_text=True)


def test_change_password_requires_csrf(client, student, login):
    login(student.student_no)
    resp = client.post(
        "/auth/change-password",
        data={
            "current_password": DEFAULT_PASSWORD,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
    )
    assert resp.status_code == 400


def test_admin_cannot_be_locked_out_of_reset_flow(client, admin, reset_request, db_session):
    """管理员处理完别人的申请后，自己的会话不受影响。"""
    _login(client, admin.student_no)
    token = _token(client, "/admin/password-resets")
    client.post(
        f"/admin/password-resets/{reset_request}/resolve", data={"csrf_token": token}
    )
    assert client.get("/admin/").status_code == 200
    assert client.get("/dashboard").status_code == 200
