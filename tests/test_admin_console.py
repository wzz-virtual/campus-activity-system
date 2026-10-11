"""F8：分类与用户管理。

产品文档要求「角色只能由管理员在后台设置」，所以这两块的管理入口本身也必须
只有管理员能进——否则「谁能改角色」这件事就没有闭环。
"""

import pytest

from app.config import MIN_PASSWORD_LENGTH
from app.models import Category


def post(client, csrf_token, path, follow_redirects=False, **data):
    payload = {"csrf_token": csrf_token("/dashboard")}
    payload.update(data)
    return client.post(path, data=payload, follow_redirects=follow_redirects)


@pytest.fixture
def admin_client(client, login, admin):
    login(admin.student_no)
    return client


# ---------------------------------------------------------------------------
# 分类管理
# ---------------------------------------------------------------------------


def test_admin_can_create_and_rename_category(admin_client, csrf_token, app):
    post(admin_client, csrf_token, "/admin/categories", name="文体", sort_order="1")

    with app.app_context():
        from app.extensions import db as _db

        category = _db.session.execute(
            _db.select(Category).where(Category.name == "文体")
        ).scalar_one()
        category_id = category.id

    post(admin_client, csrf_token, f"/admin/categories/{category_id}", name="文体活动", sort_order="2")

    with app.app_context():
        from app.extensions import db as _db

        renamed = _db.session.get(Category, category_id)
        assert renamed.name == "文体活动"
        assert renamed.sort_order == 2


def test_category_name_must_be_unique(admin_client, csrf_token, category, app):
    resp = post(
        admin_client, csrf_token, "/admin/categories", name=category.name,
        follow_redirects=True,
    )
    assert "已经存在" in resp.get_data(as_text=True)


def test_category_cannot_be_deleted_while_still_in_use(
    admin_client, csrf_token, category, teacher, activity_factory, app
):
    """删除前必须校验引用——直接删会让那些活动的分类指向一个不存在的记录。"""
    activity_factory(teacher.id, category_id=category.id)

    resp = post(
        admin_client,
        csrf_token,
        f"/admin/categories/{category.id}/delete",
        follow_redirects=True,
    )
    assert "不能删除" in resp.get_data(as_text=True)

    with app.app_context():
        from app.extensions import db as _db

        assert _db.session.get(Category, category.id) is not None


def test_unused_category_can_be_deleted(admin_client, csrf_token, app):
    post(admin_client, csrf_token, "/admin/categories", name="临时分类")

    with app.app_context():
        from app.extensions import db as _db

        category = _db.session.execute(
            _db.select(Category).where(Category.name == "临时分类")
        ).scalar_one()
        category_id = category.id

    post(admin_client, csrf_token, f"/admin/categories/{category_id}/delete")

    with app.app_context():
        from app.extensions import db as _db

        assert _db.session.get(Category, category_id) is None


def test_non_admin_cannot_touch_categories(client, login, csrf_token, teacher):
    login(teacher.student_no)
    assert client.get("/admin/categories").status_code == 403
    assert post(client, csrf_token, "/admin/categories", name="偷偷加的").status_code == 403


# ---------------------------------------------------------------------------
# 用户管理
# ---------------------------------------------------------------------------


def test_admin_creates_account_and_it_can_log_in(admin_client, csrf_token, client, login, app):
    """系统不开放自助注册，账号由管理员创建后发放。"""
    post(
        admin_client,
        csrf_token,
        "/admin/users",
        student_no="20260101",
        name="新同学",
        role="STUDENT",
        email="new@example.com",
        password="Brand-New-Pass-2026",
    )

    with app.app_context():
        from app.extensions import db as _db
        from app.models import User

        created = _db.session.execute(
            _db.select(User).where(User.student_no == "20260101")
        ).scalar_one()
        # 密码必须是哈希存储的，不能是明文
        assert created.password_hash != "Brand-New-Pass-2026"

    resp = login("20260101", password="Brand-New-Pass-2026")
    assert resp.status_code == 302
    assert "/dashboard" in resp.headers["Location"]


def test_short_initial_password_is_rejected(admin_client, csrf_token, app):
    resp = post(
        admin_client,
        csrf_token,
        "/admin/users",
        student_no="20260102",
        name="短密码同学",
        role="STUDENT",
        password="1234567",
        follow_redirects=True,
    )
    assert f"不能少于 {MIN_PASSWORD_LENGTH} 个字符" in resp.get_data(as_text=True)

    with app.app_context():
        from app.extensions import db as _db
        from app.models import User

        assert _db.session.execute(
            _db.select(User).where(User.student_no == "20260102")
        ).scalar_one_or_none() is None


def test_duplicate_student_no_is_rejected(admin_client, csrf_token, student):
    resp = post(
        admin_client,
        csrf_token,
        "/admin/users",
        student_no=student.student_no,
        name="冒名者",
        role="STUDENT",
        password="Another-Pass-2026",
        follow_redirects=True,
    )
    assert "已经存在" in resp.get_data(as_text=True)


def test_admin_can_change_role(admin_client, csrf_token, student, app):
    post(
        admin_client,
        csrf_token,
        f"/admin/users/{student.id}",
        role="TEACHER",
        status="1",
        club_name="摄影协会",
    )

    with app.app_context():
        from app.extensions import db as _db
        from app.models import User

        updated = _db.session.get(User, student.id)
        assert updated.role == "TEACHER"
        assert updated.club_name == "摄影协会"


def test_disabled_account_cannot_log_in(admin_client, csrf_token, student, login):
    post(admin_client, csrf_token, f"/admin/users/{student.id}", role="STUDENT", status="0")

    resp = login(student.student_no)
    assert resp.status_code == 403
    assert "已被停用" in resp.get_data(as_text=True)


def test_disabling_an_account_keeps_its_enrollments(
    admin_client, csrf_token, student, teacher, activity_factory, enroll_factory, app
):
    """停用后历史报名与签到记录保留，统计口径不会因为有人离校就变样。"""
    activity = activity_factory(teacher.id)
    enroll_factory(activity.id, [student.id], checked_in=[student.id])

    post(admin_client, csrf_token, f"/admin/users/{student.id}", role="STUDENT", status="0")

    with app.app_context():
        from app.extensions import db as _db
        from app.models import Enrollment

        rows = _db.session.execute(
            _db.select(Enrollment).where(Enrollment.user_id == student.id)
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].check_in_at is not None


# ---------------------------------------------------------------------------
# 把自己锁在门外的两道护栏
# ---------------------------------------------------------------------------


def test_admin_cannot_disable_or_demote_themselves(admin_client, csrf_token, admin, app):
    resp = post(
        admin_client,
        csrf_token,
        f"/admin/users/{admin.id}",
        role="STUDENT",
        status="1",
        follow_redirects=True,
    )
    assert "不能降级或停用自己" in resp.get_data(as_text=True)

    with app.app_context():
        from app.extensions import db as _db
        from app.models import User

        assert _db.session.get(User, admin.id).role == "ADMIN"


def test_system_always_keeps_at_least_one_active_admin(
    admin_client, csrf_token, admin, user_factory, app
):
    """「不能把自己停用」这条规则的直接结果：系统里永远留着至少一个管理员。

    两个管理员时，A 可以把 B 降级；但 A 动不了自己，所以 A 一定是安全的那个。
    没有这条护栏，管理员点错一下系统就没人能管了，而且不可逆——
    重装数据库才能救回来。
    """
    other = user_factory("admin2", "另一位管理员", "ADMIN")

    post(admin_client, csrf_token, f"/admin/users/{other.id}", role="STUDENT", status="1")

    with app.app_context():
        from app.extensions import db as _db
        from app.models import Role, User

        active_admins = _db.session.execute(
            _db.select(User).where(User.role == Role.ADMIN, User.status == 1)
        ).scalars().all()

        assert len(active_admins) >= 1
        assert any(person.id == admin.id for person in active_admins)
        # 被降级的那个人确实降下去了，说明这次操作本身是生效的
        assert _db.session.get(User, other.id).role == "STUDENT"


def test_non_admin_cannot_manage_users(client, login, csrf_token, teacher, student):
    login(teacher.student_no)
    assert client.get("/admin/users").status_code == 403
    assert post(client, csrf_token, f"/admin/users/{student.id}", role="ADMIN").status_code == 403

    with client.application.app_context():
        from app.extensions import db as _db
        from app.models import User

        assert _db.session.get(User, student.id).role == "STUDENT"


# ---------------------------------------------------------------------------
# 角色只能由服务端决定（复述一遍，防止新入口绕过）
# ---------------------------------------------------------------------------


def test_admin_cannot_escalate_via_form_parameter(client, login, csrf_token, teacher, app):
    """社团在请求体里塞 role=ADMIN 建账号，不会有任何效果。

    拦截发生在读表单参数之前，所以这里拿到的只能是 403——权限判断的输入
    永远不包括请求体里的角色字段。
    """
    login(teacher.student_no)
    resp = post(
        client, csrf_token, "/admin/users", student_no="hacker", name="hacker",
        role="ADMIN", password="Whatever-Pass-2026",
    )
    assert resp.status_code == 403

    with app.app_context():
        from app.extensions import db as _db
        from app.models import User

        assert _db.session.execute(
            _db.select(User).where(User.student_no == "hacker")
        ).scalar_one_or_none() is None
