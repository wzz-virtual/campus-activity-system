"""站内邮件与公告（V2.0）。

* **只有活动组织教师与系统管理员能发邮件**——学生拿不到写邮件页（403），
  POST 也会被服务端拦下（前端藏按钮不算权限控制）；
* 收件范围解析：全体用户 / 全体学生 / 全体教师 / 指定用户；
* 未读计数（导航栏角标）与已读标记；
* 一封信只有**收件人本人或发件人**能看；
* 公告：首页展示、教师 / 管理员可改、学生改不了。
"""

import pytest

from app.extensions import db as _db
from app.mailbox import DEFAULT_ANNOUNCEMENT
from app.models import Announcement, Message, MessageRecipient, Role, User


def compose(client, csrf_token, **overrides):
    data = {
        "csrf_token": csrf_token("/mail/compose"),
        "scope": "students",
        "subject": "活动安排提醒",
        "body": "本周活动安排见活动广场。",
        "user_id": "",
    }
    data.update(overrides)
    return client.post("/mail/compose", data=data, follow_redirects=True)


def count(model):
    return _db.session.execute(
        _db.select(_db.func.count()).select_from(model)
    ).scalar_one()


def recipients_of(app, subject):
    with app.app_context():
        message = _db.session.execute(
            _db.select(Message).where(Message.subject == subject)
        ).scalar_one()
        rows = _db.session.execute(
            _db.select(User.student_no)
            .join(MessageRecipient, MessageRecipient.user_id == User.id)
            .where(MessageRecipient.message_id == message.id)
        ).scalars().all()
        return sorted(rows)


# ---------------------------------------------------------------------------
# 公告
# ---------------------------------------------------------------------------


def test_homepage_has_no_announcement_by_default(client, app):
    """公告默认为空（用户要求首页不放说明文字）：不渲染，但公告行存在且只有一行。"""
    html = client.get("/").get_data(as_text=True)

    assert "把校园活动从" not in html

    with app.app_context():
        assert count(Announcement) == 1, "公告行应当被自动创建且只有一行"
        assert _db.session.get(Announcement, 1).content == ""


def test_teacher_and_admin_can_edit_announcement(client, login, csrf_token, teacher, app):
    login(teacher.student_no)
    resp = client.post(
        "/mail/announcement",
        data={"csrf_token": csrf_token("/mail/"), "content": "期末考试周活动暂停，恢复时间另行通知。"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "公告已更新" in resp.get_data(as_text=True)

    # 首页与邮件页同时生效
    homepage = client.get("/").get_data(as_text=True)
    assert "期末考试周活动暂停" in homepage
    assert "把校园活动从" not in homepage

    with app.app_context():
        row = _db.session.get(Announcement, 1)
        assert row.content == "期末考试周活动暂停，恢复时间另行通知。"
        assert row.updated_by == teacher.id


def test_student_cannot_edit_announcement(client, login, csrf_token, student, app):
    login(student.student_no)
    resp = client.post(
        "/mail/announcement",
        data={"csrf_token": csrf_token("/mail/"), "content": "学生改的公告"},
    )
    assert resp.status_code == 403

    with app.app_context():
        assert _db.session.get(Announcement, 1).content == DEFAULT_ANNOUNCEMENT


def test_empty_announcement_clears_it(client, login, csrf_token, teacher, app):
    """公告允许清空：留空保存 = 首页不再显示。"""
    login(teacher.student_no)
    resp = client.post(
        "/mail/announcement",
        data={"csrf_token": csrf_token("/mail/"), "content": "临时公告"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    resp = client.post(
        "/mail/announcement",
        data={"csrf_token": csrf_token("/mail/"), "content": "   "},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "公告已更新" in resp.get_data(as_text=True)

    with app.app_context():
        assert _db.session.get(Announcement, 1).content == ""


def test_legacy_default_announcement_is_cleared_on_boot(client, app):
    """老库里「没人改过、仍是旧默认文案」的公告，启动时被清空；教师写的不动。"""
    from app.mailbox import LEGACY_DEFAULT_ANNOUNCEMENT, normalize_legacy_announcement

    client.get("/")  # 触发公告行的自动创建

    with app.app_context():
        row = _db.session.get(Announcement, 1)
        assert row is not None
        row.content = LEGACY_DEFAULT_ANNOUNCEMENT
        _db.session.commit()

        assert normalize_legacy_announcement() is True
        row = _db.session.get(Announcement, 1)
        assert row.content == ""
        assert row.updated_by is None and row.updated_at is None

        # 教师 / 管理员自己写过的内容一字不动
        row.content = "教师自己写的公告"
        _db.session.commit()
        assert normalize_legacy_announcement() is False
        assert _db.session.get(Announcement, 1).content == "教师自己写的公告"


# ---------------------------------------------------------------------------
# 发信的权限边界
# ---------------------------------------------------------------------------


def test_student_cannot_open_compose_or_send(client, login, csrf_token, student, app):
    """学生只收信：写邮件页 403，直接 POST 也 403，一条记录都不会产生。"""
    login(student.student_no)

    assert client.get("/mail/compose").status_code == 403
    assert client.post(
        "/mail/compose",
        data={"csrf_token": csrf_token("/mail/"), "scope": "all",
              "subject": "学生想群发", "body": "你好"},
    ).status_code == 403

    with app.app_context():
        assert count(Message) == 0


@pytest.mark.parametrize("role_fixture", ["teacher", "admin"])
def test_teacher_and_admin_can_open_compose(client, login, role_fixture, request):
    user = request.getfixturevalue(role_fixture)
    login(user.student_no)
    assert client.get("/mail/compose").status_code == 200


# ---------------------------------------------------------------------------
# 收件范围
# ---------------------------------------------------------------------------


def test_teacher_can_send_to_all_students(client, login, csrf_token, teacher, student,
                                          user_factory, app):
    user_factory("20230102", "王芳")
    login(teacher.student_no)

    resp = compose(client, csrf_token, subject="本周活动安排", scope="students")
    assert "已发送" in resp.get_data(as_text=True)

    assert recipients_of(app, "本周活动安排") == ["20230101", "20230102"]


def test_sending_to_specified_user_reaches_only_that_user(client, login, csrf_token,
                                                          teacher, student, user_factory, app):
    other = user_factory("20230102", "王芳")
    login(teacher.student_no)

    compose(client, csrf_token, subject="单独通知", scope="one", user_id=other.id)

    assert recipients_of(app, "单独通知") == ["20230102"]


def test_sending_to_all_users_excludes_the_sender(client, login, csrf_token, admin,
                                                  student, teacher, app):
    login(admin.student_no)
    compose(client, csrf_token, subject="全体通知", scope="all")

    recipients = recipients_of(app, "全体通知")
    assert "admin" not in recipients, "不给自己发信"
    assert "20230101" in recipients and "teacher001" in recipients


def test_compose_validation(client, login, csrf_token, teacher, app):
    login(teacher.student_no)

    resp = compose(client, csrf_token, subject="")
    assert "主题不能为空" in resp.get_data(as_text=True)

    resp = compose(client, csrf_token, body="")
    assert "正文不能为空" in resp.get_data(as_text=True)

    resp = compose(client, csrf_token, scope="one", user_id="")
    assert "指定的收件人不存在" in resp.get_data(as_text=True)

    with app.app_context():
        assert count(Message) == 0, "校验失败不能留下半封信"


# ---------------------------------------------------------------------------
# 收件箱与已读
# ---------------------------------------------------------------------------


def test_student_sees_unread_mail_and_it_becomes_read(client, login, csrf_token,
                                                      teacher, student, app):
    login(teacher.student_no)
    compose(client, csrf_token, subject="人像摄影体验日说明", scope="students")

    login(student.student_no)
    inbox_html = client.get("/mail/").get_data(as_text=True)
    assert "人像摄影体验日说明" in inbox_html
    assert "未读" in inbox_html
    assert "收件箱（1）" in inbox_html

    # 导航栏角标：未读 1 封
    dashboard = client.get("/dashboard").get_data(as_text=True)
    assert "邮件" in dashboard
    assert '<span class="badge text-bg-danger">1</span>' in dashboard

    with app.app_context():
        message_id = _db.session.execute(
            _db.select(Message.id).where(Message.subject == "人像摄影体验日说明")
        ).scalar_one()

    # 打开即已读
    detail = client.get(f"/mail/{message_id}").get_data(as_text=True)
    assert "人像摄影体验日说明" in detail
    assert "本周活动" in detail

    after = client.get("/mail/").get_data(as_text=True)
    assert "已读" in after
    assert "收件箱（1）" in after

    with app.app_context():
        record = _db.session.execute(
            _db.select(MessageRecipient).where(
                MessageRecipient.message_id == message_id,
                MessageRecipient.user_id == student.id,
            )
        ).scalar_one()
        assert record.read_at is not None


def test_message_is_only_visible_to_recipient_and_sender(client, login, csrf_token,
                                                         teacher, student, user_factory, app):
    """不是收件人也不是发件人：这封信当作不存在（404，而不是 403）。"""
    other = user_factory("20230102", "王芳")

    login(teacher.student_no)
    compose(client, csrf_token, subject="只给一人", scope="one", user_id=student.id)

    with app.app_context():
        message_id = _db.session.execute(
            _db.select(Message.id).where(Message.subject == "只给一人")
        ).scalar_one()

    # 收件人：能看
    login(student.student_no)
    assert client.get(f"/mail/{message_id}").status_code == 200

    # 发件人：能看
    login(teacher.student_no)
    assert client.get(f"/mail/{message_id}").status_code == 200

    # 无关的人：404
    login(other.student_no)
    assert client.get(f"/mail/{message_id}").status_code == 404


def test_sender_sees_sent_list(client, login, csrf_token, teacher, student, app):
    login(teacher.student_no)
    compose(client, csrf_token, subject="发件箱可见", scope="students")

    html = client.get("/mail/").get_data(as_text=True)
    assert "我发出的（1）" in html
    assert "发件箱可见" in html


def test_unread_badge_counts_only_my_unread_mail(client, login, csrf_token, admin,
                                                 student, user_factory, app):
    other = user_factory("20230102", "王芳")

    # 管理员发两封给全体学生
    login(admin.student_no)
    compose(client, csrf_token, subject="通知一", scope="students")
    compose(client, csrf_token, subject="通知二", scope="students")

    # 学生：未读 2
    login(student.student_no)
    assert '<span class="badge text-bg-danger">2</span>' in client.get("/dashboard").get_data(as_text=True)

    # 王芳：未读也是 2（每个人都独立计数）
    login(other.student_no)
    assert '<span class="badge text-bg-danger">2</span>' in client.get("/dashboard").get_data(as_text=True)
