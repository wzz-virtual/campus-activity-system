"""开放注册（V2.0）。

三条硬规则：

1. **同一个学号 / 工号只能注册一个账号**——重复注册被拒绝；
2. **密码以首次注册时输入的为准**——重复注册绝不会覆盖已有账号的密码，
   也（配合唯一索引）拦得住并发抢注；
3. **角色写死为学生**——注册接口不接受任何角色输入，教师 / 管理员
   账号只能由管理员创建。
"""

from app.extensions import db as _db
from app.models import Role, User

from conftest import DEFAULT_PASSWORD

NEW_PASSWORD = "My-First-Pass-2026"


def register(client, csrf_token, **overrides):
    # 注意：follow_redirects 是给测试客户端的参数，不能混进表单数据里
    follow = overrides.pop("follow_redirects", False)
    data = {
        "csrf_token": csrf_token("/auth/register"),
        "student_no": "20269901",
        "name": "新同学",
        "password": NEW_PASSWORD,
        "confirm_password": NEW_PASSWORD,
    }
    data.update(overrides)
    return client.post("/auth/register", data=data, follow_redirects=follow)


def find(app, student_no):
    with app.app_context():
        return _db.session.execute(
            _db.select(User).where(User.student_no == student_no)
        ).scalar_one_or_none()


def test_register_creates_student_account_and_signs_in(client, csrf_token, app):
    resp = register(client, csrf_token, follow_redirects=True)

    assert resp.status_code == 200
    assert "注册成功" in resp.get_data(as_text=True)

    user = find(app, "20269901")
    assert user is not None
    assert user.role == Role.STUDENT, "自助注册只能创建学生账号"
    assert user.check_password(NEW_PASSWORD), "密码要以注册时输入的为准"
    assert user.password_hash != NEW_PASSWORD, "密码必须哈希存储"

    # 注册成功直接进入登录态
    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "新同学" in dashboard.get_data(as_text=True)


def test_registered_account_can_log_in_later(client, csrf_token, login, app):
    register(client, csrf_token)

    # 退出后用新账号重新登录
    resp = login("20269901", password=NEW_PASSWORD)
    assert resp.status_code == 302
    assert "/dashboard" in resp.headers["Location"]


def test_duplicate_student_no_is_rejected_and_original_password_kept(
    client, csrf_token, student, app
):
    """同一个学号重复注册：被拒绝，且**绝不覆盖**已有账号的密码。"""
    resp = register(
        client,
        csrf_token,
        student_no=student.student_no,
        name="冒名者",
        password="Another-Pass-2026",
        confirm_password="Another-Pass-2026",
    )

    assert resp.status_code == 400
    assert "已经注册过" in resp.get_data(as_text=True)

    user = find(app, student.student_no)
    assert user.name == student.name, "已有账号的姓名被改了"
    assert user.check_password(DEFAULT_PASSWORD), "已有账号的密码被覆盖了"
    assert not user.check_password("Another-Pass-2026")


def test_register_cannot_force_a_role(client, csrf_token, app):
    """请求体里塞 role=ADMIN 没有任何效果——角色只能由服务端决定。"""
    client.post(
        "/auth/register",
        data={
            "csrf_token": csrf_token("/auth/register"),
            "student_no": "hacker001",
            "name": "想当管理员的同学",
            "password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
            "role": "ADMIN",
            "is_admin": "1",
        },
    )

    user = find(app, "hacker001")
    assert user is not None and user.role == Role.STUDENT


def test_register_form_validation(client, csrf_token, app):
    cases = [
        ({"student_no": ""}, "学号 / 工号不能为空"),
        ({"name": ""}, "姓名不能为空"),
        ({"password": "1234567", "confirm_password": "1234567"}, "密码长度不能少于"),
        ({"confirm_password": "Mismatch-Pass-2026"}, "两次输入的密码不一致"),
    ]
    for overrides, expected in cases:
        resp = register(client, csrf_token, **overrides)
        assert resp.status_code == 400, overrides
        assert expected in resp.get_data(as_text=True), overrides

    # 一条都没落库
    with app.app_context():
        assert _db.session.execute(
            _db.select(_db.func.count()).select_from(User).where(User.student_no == "20269901")
        ).scalar_one() == 0


def test_register_without_csrf_token_is_rejected(client, app):
    resp = client.post(
        "/auth/register",
        data={"student_no": "20269902", "name": "无令牌", "password": NEW_PASSWORD,
              "confirm_password": NEW_PASSWORD},
    )
    assert resp.status_code == 400
    assert find(app, "20269902") is None


def test_disabled_account_cannot_be_re_registered(client, csrf_token, user_factory, app):
    """被停用的账号也占着这个学号——重新注册不会把它"洗白"成新账号。"""
    user_factory("20269903", "被停用的同学", active=False)

    resp = register(
        client, csrf_token, student_no="20269903", name="想复活的同学"
    )
    assert resp.status_code == 400
    assert "已经注册过" in resp.get_data(as_text=True)
