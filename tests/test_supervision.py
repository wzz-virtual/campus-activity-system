"""管理员的活动监督：下线 / 恢复（FR-8.3 / TC-14）。

V2.0 里管理员在活动上的**全部权力**就是这两件事：

* 下线——学生端不再展示、不能报名；教师端仍可见，并带「处理标记」；
* 恢复——按时间重新定位到 已发布 / 进行中 / 已结束。

不做的事同样重要：不删数据、不改内容、不动报名记录（DEC-5）。
"""

import pytest

from app import services
from app.extensions import db as _db
from app.models import Activity, ActivityStatus, AuditLog


def post(client, csrf_token, path, follow_redirects=False, **data):
    payload = {"csrf_token": csrf_token("/dashboard")}
    payload.update(data)
    return client.post(path, data=payload, follow_redirects=follow_redirects)


@pytest.fixture
def admin_client(client, login, admin):
    login(admin.student_no)
    return client


def test_admin_can_offline_a_published_activity(
    admin_client, csrf_token, admin, teacher, activity_factory, activity_state, app
):
    activity = activity_factory(teacher.id, title="违规活动", status=ActivityStatus.PUBLISHED)

    resp = post(
        admin_client,
        csrf_token,
        f"/admin/activities/{activity.id}/offline",
        reason="内容与校历冲突",
        follow_redirects=True,
    )

    assert "已下线" in resp.get_data(as_text=True)
    assert activity_state(activity.id).status == ActivityStatus.OFFLINE

    # 先写旧接口不行——这里再确认处理标记真的落进了日志表
    with app.app_context():
        log = _db.session.execute(_db.select(AuditLog)).scalars().first()
        assert log is not None
        assert log.action == "OFFLINE"
        assert log.reason == "内容与校历冲突"
        assert log.operator_id == admin.id


def test_tc14_offlined_activity_is_hidden_and_unenrollable(
    admin_client, csrf_token, client, login, student, teacher, activity_factory, app
):
    """TC-14：下线后学生端不再展示、不能报名。"""
    activity = activity_factory(teacher.id, title="违规活动", status=ActivityStatus.PUBLISHED)

    post(
        admin_client,
        csrf_token,
        f"/admin/activities/{activity.id}/offline",
        reason="违规",
    )

    login(student.student_no)
    plaza = client.get("/activities").get_data(as_text=True)
    assert "违规活动" not in plaza
    assert client.get(f"/activities/{activity.id}").status_code == 404

    # 即使直接 POST 报名也会被拒
    resp = post(
        client,
        csrf_token,
        f"/activities/{activity.id}/enroll",
        follow_redirects=True,
    )
    assert "已被管理员下线" in resp.get_data(as_text=True)


def test_tc14_offline_mark_is_visible_to_the_teacher(
    admin_client, csrf_token, client, login, teacher, activity_factory, app
):
    """TC-14：教师端仍可见（含处理标记）——谁、什么时候、为什么。"""
    activity = activity_factory(teacher.id, title="违规活动", status=ActivityStatus.PUBLISHED)

    post(
        admin_client,
        csrf_token,
        f"/admin/activities/{activity.id}/offline",
        reason="与校运会冲突，待核实",
    )

    login(teacher.student_no)

    detail = client.get(f"/activities/{activity.id}").get_data(as_text=True)
    assert "已被管理员下线" in detail
    assert "与校运会冲突，待核实" in detail

    mine = client.get("/my/activities").get_data(as_text=True)
    assert "已被管理员下线" in mine
    assert "与校运会冲突，待核实" in mine


def test_admin_can_restore_and_status_is_recomputed_by_time(
    admin_client, csrf_token, teacher, activity_factory, activity_state, mutate
):
    """恢复不是简单回到「已发布」——要按时间重新定位。

    被下线期间活动可能早就开始了，恢复后不能还挂着「已发布」等人来报名。
    """
    activity = activity_factory(
        teacher.id,
        status=ActivityStatus.PUBLISHED,
        # 开始时间已经过去、结束时间还没到 → 恢复后应当是「进行中」
        start_in_hours=-1,
        end_in_hours=3,
        deadline_in_hours=-2,
    )
    post(admin_client, csrf_token, f"/admin/activities/{activity.id}/offline")

    resp = post(
        admin_client,
        csrf_token,
        f"/admin/activities/{activity.id}/restore",
        follow_redirects=True,
    )

    assert "恢复" in resp.get_data(as_text=True)
    assert activity_state(activity.id).status == ActivityStatus.ONGOING


def test_offline_only_from_live_states(app, admin, teacher, activity_factory):
    """只有对外展示中的活动才需要下线：草稿、已取消没有这个必要。"""
    draft = activity_factory(teacher.id, status=ActivityStatus.DRAFT)
    canceled = activity_factory(teacher.id, status=ActivityStatus.CANCELED)

    with app.app_context():
        for activity in (draft, canceled):
            current = _db.session.get(Activity, activity.id)
            ok, message = services.offline_activity(current, admin.id, "理由")
            assert not ok
            assert "不能下线" in message


def test_restore_only_from_offline(app, admin, teacher, activity_factory):
    published = activity_factory(teacher.id, status=ActivityStatus.PUBLISHED)
    with app.app_context():
        current = _db.session.get(Activity, published.id)
        ok, message = services.restore_activity(current, admin.id)
        assert not ok
        assert "只有已下线的活动可以恢复" in message


def test_non_admin_cannot_offline_or_restore(
    client, login, csrf_token, teacher, student, activity_factory
):
    """下线 / 恢复是管理员的专属工具：教师和学生都不行——包括对自己活动。"""
    activity = activity_factory(teacher.id, status=ActivityStatus.PUBLISHED)

    login(teacher.student_no)
    assert post(
        client, csrf_token, f"/admin/activities/{activity.id}/offline"
    ).status_code == 403

    login(student.student_no)
    assert post(
        client, csrf_token, f"/admin/activities/{activity.id}/offline"
    ).status_code == 403


def test_offline_does_not_touch_enrollments_or_content(
    admin_client, csrf_token, teacher, student, activity_factory, enroll_factory, app
):
    """DEC-5：下线只改展示状态——不删数据、不改内容、不动报名记录。"""
    activity = activity_factory(teacher.id, title="违规活动", capacity=10)
    enroll_factory(activity.id, [student.id])

    with app.app_context():
        before = _db.session.get(Activity, activity.id)
        title_before = before.title
        count_before = before.enrolled_count

    post(admin_client, csrf_token, f"/admin/activities/{activity.id}/offline", reason="违规")

    with app.app_context():
        after = _db.session.get(Activity, activity.id)
        assert after.title == title_before
        assert after.enrolled_count == count_before, "报名记录与计数一概不动"
        assert after.status == ActivityStatus.OFFLINE


def test_supervision_page_lists_all_activities(admin_client, teacher, activity_factory):
    activity_factory(teacher.id, title="待监督的活动", status=ActivityStatus.PUBLISHED)
    html = admin_client.get("/admin/activities").get_data(as_text=True)

    assert "活动监督" in html
    assert "待监督的活动" in html
    # 管理员的列表页不该出现「名单 / 统计」这类会 403 的入口
    assert "名单与签到" not in html


def test_offline_requires_a_reason_field_but_allows_empty(
    admin_client, csrf_token, teacher, activity_factory, activity_state, app
):
    """下线原因可选——离线操作本身不能被"没填原因"挡住。"""
    activity = activity_factory(teacher.id, status=ActivityStatus.PUBLISHED)
    resp = post(admin_client, csrf_token, f"/admin/activities/{activity.id}/offline")

    assert resp.status_code == 302
    assert activity_state(activity.id).status == ActivityStatus.OFFLINE
