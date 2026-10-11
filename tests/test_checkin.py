"""M4：签到核验。

两条通道都要覆盖：学生凭 6 位签到码自助签到，组织者在名单上手动勾选。
"""

import pytest

from app.extensions import db as _db
from app.models import Activity, ActivityStatus, Enrollment, EnrollStatus, utcnow


def post(client, csrf_token, path, follow_redirects=False, **data):
    payload = {"csrf_token": csrf_token("/dashboard")}
    payload.update(data)
    return client.post(path, data=payload, follow_redirects=follow_redirects)


@pytest.fixture
def ongoing(teacher, activity_factory, mutate):
    """一场已经进入「进行中」、签到码为 482913 的活动。"""
    activity = activity_factory(
        teacher.id,
        title="手机摄影技巧分享会",
        status=ActivityStatus.ONGOING,
        start_in_hours=-1,
        end_in_hours=2,
        deadline_in_hours=-2,
    )

    def _arm(session):
        row = session.get(Activity, activity.id)
        row.status = ActivityStatus.ONGOING
        row.check_in_code = "482913"

    mutate(_arm)
    return activity


# ---------------------------------------------------------------------------
# 凭码签到
# ---------------------------------------------------------------------------


def test_check_in_with_correct_code(client, login, csrf_token, student, ongoing, enroll_factory):
    # 活动已经是「进行中」，报名通道关了，所以直接铺一条报名记录
    enroll_factory(ongoing.id, [student.id])
    login(student.student_no)

    resp = post(
        client, csrf_token, f"/activities/{ongoing.id}/check-in", code="482913",
        follow_redirects=True,
    )

    assert "签到成功" in resp.get_data(as_text=True)
    with client.application.app_context():
        row = _db.session.execute(
            _db.select(Enrollment).where(Enrollment.activity_id == ongoing.id)
        ).scalar_one()
        assert row.check_in_at is not None
        # 自助签到没有核验人
        assert row.check_in_by is None


def test_check_in_with_wrong_code_is_rejected(client, login, csrf_token, student, ongoing, enroll_factory):
    enroll_factory(ongoing.id, [student.id])
    login(student.student_no)

    resp = post(
        client, csrf_token, f"/activities/{ongoing.id}/check-in", code="000000",
        follow_redirects=True,
    )
    assert "签到码不正确" in resp.get_data(as_text=True)


def test_check_in_requires_an_enrollment(client, login, csrf_token, student, ongoing):
    """没报名就不能签到：签到信息挂在报名记录上，两者一对一。"""
    login(student.student_no)
    resp = post(
        client, csrf_token, f"/activities/{ongoing.id}/check-in", code="482913",
        follow_redirects=True,
    )
    assert "你没有报名这个活动" in resp.get_data(as_text=True)


def test_repeated_check_in_is_rejected(client, login, csrf_token, student, ongoing, enroll_factory):
    enroll_factory(ongoing.id, [student.id])
    login(student.student_no)
    post(client, csrf_token, f"/activities/{ongoing.id}/check-in", code="482913")

    resp = post(
        client, csrf_token, f"/activities/{ongoing.id}/check-in", code="482913",
        follow_redirects=True,
    )
    assert "你已经签到过了" in resp.get_data(as_text=True)


@pytest.mark.parametrize("status", ["PUBLISHED", "FINISHED", "CANCELED", "DRAFT"])
def test_check_in_requires_ongoing_status(client, login, csrf_token, student, teacher, activity_factory, mutate, status):
    """只有「进行中」才开放签到。"""
    from app import services

    activity = activity_factory(teacher.id, status=status, start_in_hours=-1, end_in_hours=2)
    with client.application.app_context():
        row = _db.session.get(Activity, activity.id)
        row.status = status
        row.check_in_code = "482913"
        _db.session.commit()

        # 先造一条报名记录（直接写库，绕开报名本身的校验）
        _db.session.add(
            Enrollment(
                activity_id=activity.id,
                user_id=student.id,
                status=EnrollStatus.CONFIRMED,
                enrolled_at=utcnow(),
            )
        )
        _db.session.commit()

        ok, message = services.check_in_by_code(activity.id, student.id, "482913")

    assert not ok
    assert message == "活动未开始或已结束，签到通道已关闭"


def test_cancelled_enrollment_cannot_check_in(client, login, csrf_token, student, ongoing, enroll_factory):
    enroll_factory(ongoing.id, [student.id])

    # 报名成功后活动已经进入进行中，取消通道是关的，直接改库模拟「报名已取消」
    with client.application.app_context():
        row = _db.session.execute(
            _db.select(Enrollment).where(Enrollment.activity_id == ongoing.id)
        ).scalar_one()
        row.status = EnrollStatus.CANCELED
        _db.session.commit()

    login(student.student_no)
    resp = post(
        client, csrf_token, f"/activities/{ongoing.id}/check-in", code="482913",
        follow_redirects=True,
    )
    assert "你没有报名这个活动" in resp.get_data(as_text=True)


def test_check_in_page_explains_why_it_is_closed(client, login, student, teacher, activity_factory):
    activity = activity_factory(teacher.id, status=ActivityStatus.PUBLISHED, start_in_hours=48)
    login(student.student_no)

    page = client.get(f"/activities/{activity.id}/check-in").get_data(as_text=True)
    assert "活动还没开始" in page


# ---------------------------------------------------------------------------
# 组织者手动核验
# ---------------------------------------------------------------------------


def test_organizer_can_mark_and_undo_check_in(
    client, login, csrf_token, teacher, student, ongoing, enroll_factory
):
    """手动勾选与撤销误操作都要能用。"""
    enroll_factory(ongoing.id, [student.id])
    login(teacher.student_no)

    # 勾上
    post(
        client,
        csrf_token,
        f"/activities/{ongoing.id}/enrollments/{student.id}/check-in",
        checked_in="1",
    )
    with client.application.app_context():
        row = _db.session.execute(
            _db.select(Enrollment).where(Enrollment.activity_id == ongoing.id)
        ).scalar_one()
        assert row.check_in_at is not None
        assert row.check_in_by == teacher.id  # 手动核验要记下是谁操作的

    # 撤销
    post(
        client,
        csrf_token,
        f"/activities/{ongoing.id}/enrollments/{student.id}/check-in",
        checked_in="0",
    )
    with client.application.app_context():
        row = _db.session.execute(
            _db.select(Enrollment).where(Enrollment.activity_id == ongoing.id)
        ).scalar_one()
        assert row.check_in_at is None
        assert row.check_in_by is None


def test_manual_check_in_requires_ongoing_status(
    client, login, csrf_token, teacher, student, activity_factory, enroll_factory
):
    activity = activity_factory(teacher.id, status=ActivityStatus.PUBLISHED, start_in_hours=48)
    enroll_factory(activity.id, [student.id])

    login(teacher.student_no)
    resp = post(
        client,
        csrf_token,
        f"/activities/{activity.id}/enrollments/{student.id}/check-in",
        checked_in="1",
        follow_redirects=True,
    )
    assert "只有进行中的活动才能核验签到" in resp.get_data(as_text=True)


def test_manual_check_in_requires_an_enrollment(client, login, csrf_token, teacher, student, ongoing):
    login(teacher.student_no)
    resp = post(
        client,
        csrf_token,
        f"/activities/{ongoing.id}/enrollments/{student.id}/check-in",
        checked_in="1",
        follow_redirects=True,
    )
    assert "该用户不在正式名单中" in resp.get_data(as_text=True)


def test_duplicate_manual_check_in_is_rejected(client, login, csrf_token, teacher, student, ongoing, enroll_factory):
    enroll_factory(ongoing.id, [student.id])
    login(teacher.student_no)

    path = f"/activities/{ongoing.id}/enrollments/{student.id}/check-in"
    post(client, csrf_token, path, checked_in="1")
    resp = post(client, csrf_token, path, checked_in="1", follow_redirects=True)

    assert "该用户已经签到了" in resp.get_data(as_text=True)


def test_stranger_teacher_cannot_verify_check_in(
    client, login, csrf_token, teacher, user_factory, student, ongoing, enroll_factory
):
    """核验签到是**该活动组织教师本人**的权力，别的教师碰不到。"""
    stranger = user_factory("club007", "话剧社", "TEACHER")
    enroll_factory(ongoing.id, [student.id])

    login(stranger.student_no)
    resp = post(
        client,
        csrf_token,
        f"/activities/{ongoing.id}/enrollments/{student.id}/check-in",
        checked_in="1",
    )
    assert resp.status_code == 403


def test_admin_cannot_verify_check_in_for_any_activity(
    client, login, csrf_token, admin, student, ongoing, enroll_factory
):
    """V2.0：管理员**不介入具体报名**——核验签到也不行（A-07）。

    V1.0 里管理员对全部资源放行；V2.0 的权限矩阵把这条收紧了：
    管理员对活动的工具只有「下线 / 恢复」。
    """
    enroll_factory(ongoing.id, [student.id])

    login(admin.student_no)
    resp = post(
        client,
        csrf_token,
        f"/activities/{ongoing.id}/enrollments/{student.id}/check-in",
        checked_in="1",
    )
    assert resp.status_code == 403

    with client.application.app_context():
        row = _db.session.execute(
            _db.select(Enrollment).where(Enrollment.activity_id == ongoing.id)
        ).scalar_one()
        assert row.check_in_at is None


def test_roster_page_shows_check_in_code_and_headcounts(
    client, login, csrf_token, teacher, student, ongoing, enroll_factory
):
    """名单页要有签到码，以及 FR-3.2 要求的四个数字：上限 / 正式 / 候补 / 余位。"""
    enroll_factory(ongoing.id, [student.id])

    login(teacher.student_no)
    html = client.get(f"/activities/{ongoing.id}/enrollments").get_data(as_text=True)

    assert "482913" in html
    assert "人数上限" in html
    assert "正式人数" in html
    assert "候补人数" in html
    assert "剩余名额" in html


def test_student_cannot_open_the_roster(client, login, student, ongoing):
    login(student.student_no)
    assert client.get(f"/activities/{ongoing.id}/enrollments").status_code == 403


def test_open_check_in_closes_enrollment(
    client, login, csrf_token, teacher, student, activity_factory, activity_state
):
    """开启签到会把活动推进到进行中，同时关闭报名与取消通道。"""
    activity = activity_factory(teacher.id, status=ActivityStatus.PUBLISHED, start_in_hours=6, deadline_in_hours=2)
    login(teacher.student_no)

    client.post(
        f"/activities/{activity.id}/check-in/open",
        data={"csrf_token": csrf_token("/dashboard")},
    )

    state = activity_state(activity.id)
    assert state.status == ActivityStatus.ONGOING
    assert state.check_in_code and len(state.check_in_code) == 6

    # 报名通道这时候已经关了
    import re

    html = client.get(f"/activities/{activity.id}").get_data(as_text=True)
    assert "活动已开始，报名通道已关闭" in html
    assert not re.search(r'action="/activities/\d+/enroll"', html)
