"""报名审核（V2.0 核心，FR-6 / BR-8 ~ BR-10）。

对应《实验2-需求分析.md》第八章的验收场景 TC-7 ~ TC-9。核心口径一句话：

**通过 ≠ 一定参加。** 教师"通过"的是资格；学生能不能占上名额，取决于
审批那一刻还有没有余位——有余位转「正式」，没有余位排进候补队列（TC-8）。
拒绝则是终态：本次不能参加，也**不进入候补**。

审核权限（BR-8）：只有**该活动的组织教师本人**能审。另一位教师、管理员、
学生都不行——管理员不介入具体报名（A-07）。
"""

from datetime import timedelta

import pytest

from app import services
from app.extensions import db as _db
from app.models import Activity, Enrollment, EnrollStatus, utcnow


def review_required_activity(activity_factory, teacher, **extra):
    """造一个"需审核"的活动。"""
    return activity_factory(
        teacher.id,
        requires_review=True,
        qualification="面向全体在校学生；需自备相机。",
        **extra,
    )


# ---------------------------------------------------------------------------
# 报名 → 待审核
# ---------------------------------------------------------------------------


def test_signup_for_review_activity_goes_to_pending(
    app, student, teacher, activity_factory, enrollment_state
):
    """FR-2.2：需审核的活动，提交后为「待审核」，不占名额。"""
    activity = review_required_activity(activity_factory, teacher, capacity=10)

    with app.app_context():
        ok, message = services.enroll(activity.id, student.id)

    assert ok and "审核" in message
    state = enrollment_state(activity.id, student.id)
    assert state.status == EnrollStatus.PENDING
    # 待审核不占名额：正式人数保持 0，余位一点没动
    with app.app_context():
        assert _db.session.get(Activity, activity.id).enrolled_count == 0


def test_pending_signup_is_rejected_from_signing_up_again(
    app, student, teacher, activity_factory
):
    """BR-2：待审核也是"有效报名"，不能重复提交。"""
    activity = review_required_activity(activity_factory, teacher)

    with app.app_context():
        assert services.enroll(activity.id, student.id)[0]
        ok, message = services.enroll(activity.id, student.id)

    assert not ok and "待审核" in message


# ---------------------------------------------------------------------------
# TC-7 / TC-8 / TC-9：教师处理的三条分支
# ---------------------------------------------------------------------------


def test_tc7_approve_with_room_confirms_immediately(
    app, student, teacher, activity_factory, enrollment_state
):
    """TC-7：需审核活动、有余位。教师通过 → 该学生立即「正式」。"""
    activity = review_required_activity(activity_factory, teacher, capacity=10)
    with app.app_context():
        assert services.enroll(activity.id, student.id)[0]
        ok, message = services.review_enrollment(
            activity.id, student.id, teacher.id, approve=True
        )

    assert ok and "正式" in message
    assert enrollment_state(activity.id, student.id).status == EnrollStatus.CONFIRMED
    with app.app_context():
        assert _db.session.get(Activity, activity.id).enrolled_count == 1


def test_tc8_approve_when_full_lands_in_waitlist(
    app, student, teacher, other_teacher, user_factory, activity_factory, enrollment_state
):
    """TC-8：需审核活动、已满员。教师通过 → 进入「候补」，不得转正式。

    队列按**通过的时刻**排序（TC-8 原文："队列记录通过时刻"）。
    """
    activity = review_required_activity(activity_factory, teacher, capacity=2)
    early = user_factory("20230901", "先通过的")
    later = user_factory("20230902", "后通过的")

    with app.app_context():
        _db.session.get(Activity, activity.id).enrolled_count = 2  # 已满员
        for person in (early, later, student):
            services.enroll(activity.id, person.id)
        _db.session.commit()

        services.review_enrollment(activity.id, early.id, teacher.id, approve=True)
        # 把两次通过的时间拉开，验证"按通过时刻排序"
        row = _db.session.execute(
            _db.select(Enrollment).where(
                Enrollment.activity_id == activity.id,
                Enrollment.user_id == early.id,
            )
        ).scalar_one()
        row.waitlisted_at = utcnow() - timedelta(minutes=10)
        _db.session.commit()

        ok, message = services.review_enrollment(
            activity.id, later.id, teacher.id, approve=True
        )

    assert ok and "候补" in message
    later_state = enrollment_state(activity.id, later.id)
    assert later_state.status == EnrollStatus.WAITLISTED
    assert later_state.position == 2, "后通过的人应排在候补队尾"

    with app.app_context():
        assert _db.session.get(Activity, activity.id).enrolled_count == 2, (
            "通过≠正式：已满员时通过绝不能把名额顶爆（BR-1 / BR-9）"
        )


def test_tc9_reject_is_terminal_and_never_waitlists(
    app, student, teacher, activity_factory, enrollment_state
):
    """TC-9：教师拒绝 → 该学生「已拒绝」，不能参加；**不得进入候补**。"""
    activity = review_required_activity(activity_factory, teacher, capacity=1, enrolled_count=1)
    with app.app_context():
        assert services.enroll(activity.id, student.id)[0]
        ok, message = services.review_enrollment(
            activity.id, student.id, teacher.id, approve=False
        )

    assert ok and "拒绝" in message
    state = enrollment_state(activity.id, student.id)
    assert state.status == EnrollStatus.REJECTED
    assert state.waitlisted_at is None, "被拒绝的人绝不能出现在候补队列里"
    assert state.reviewed_at is not None, "审核时间要给学生看到（FR-6.4）"


def test_approved_then_waitlisted_student_is_promoted_when_seat_frees(
    app, student, teacher, user_factory, activity_factory, enrollment_state
):
    """TC-8 的后续：通过进了候补的人，和普通候补一样享受自动递补。

    ⚠️ 截止时间放到 48 小时后：默认的 24 小时恰好压在名单锁定窗口边界上，
    "取消"会被锁定拦下，就测不到递补了。
    """
    activity = review_required_activity(
        activity_factory, teacher, capacity=1, deadline_in_hours=48
    )
    confirmed = user_factory("20230903", "占位的人")

    with app.app_context():
        _db.session.add(
            Enrollment(
                activity_id=activity.id,
                user_id=confirmed.id,
                status=EnrollStatus.CONFIRMED,
                enrolled_at=utcnow(),
            )
        )
        _db.session.get(Activity, activity.id).enrolled_count = 1
        _db.session.commit()

        services.enroll(activity.id, student.id)
        services.review_enrollment(activity.id, student.id, teacher.id, approve=True)
        # 占位的人取消 → 通过审核的候补者应自动递补
        services.cancel_enrollment(activity.id, confirmed.id)

    assert enrollment_state(activity.id, student.id).status == EnrollStatus.CONFIRMED


def test_review_after_deadline_still_confirms_if_room(
    app, student, teacher, activity_factory, enrollment_state, mutate
):
    """教师在报名截止**之后**才审核，只要还有余位就该转正式。

    这是最常见的真实场景：学生截止前提交，教师事后统一处理。
    如果这里被"报名已截止"拦住，学生会永远卡在待审核里。
    """
    activity = review_required_activity(activity_factory, teacher, capacity=10)
    with app.app_context():
        assert services.enroll(activity.id, student.id)[0]

    mutate(
        lambda s: setattr(
            s.get(Activity, activity.id), "enroll_deadline", utcnow() - timedelta(hours=1)
        )
    )

    with app.app_context():
        ok, message = services.review_enrollment(
            activity.id, student.id, teacher.id, approve=True
        )

    assert ok and "正式" in message
    assert enrollment_state(activity.id, student.id).status == EnrollStatus.CONFIRMED


# ---------------------------------------------------------------------------
# 权限：只有本人活动的组织教师能审
# ---------------------------------------------------------------------------


def test_student_can_withdraw_pending_application(
    app, student, teacher, activity_factory, enrollment_state
):
    """DEC-2 / FR-5.2：「待审核」的申请允许学生自行撤销。"""
    activity = review_required_activity(activity_factory, teacher, deadline_in_hours=1)
    with app.app_context():
        assert services.enroll(activity.id, student.id)[0]
        # 撤销不受名单锁定限制（不占名额、随时可撤）
        ok, message = services.cancel_enrollment(activity.id, student.id)

    assert ok, message
    assert enrollment_state(activity.id, student.id).status == EnrollStatus.CANCELED


def test_reapply_after_rejection_starts_a_fresh_application(
    app, student, teacher, activity_factory, enrollment_state
):
    """DEC-3：被拒后可重新报名，回到「待审核」，上一轮审核痕迹清空。"""
    activity = review_required_activity(activity_factory, teacher)
    with app.app_context():
        assert services.enroll(activity.id, student.id)[0]
        services.review_enrollment(activity.id, student.id, teacher.id, approve=False)

    with app.app_context():
        ok, _ = services.enroll(activity.id, student.id)

    assert ok
    state = enrollment_state(activity.id, student.id)
    assert state.status == EnrollStatus.PENDING
    assert state.reviewed_at is None, "新一轮申请不应带着上一轮的审核结果"


def test_only_organizer_can_review(app, client, teacher, other_teacher, admin, student,
                                   activity_factory, login, csrf_token):
    """BR-8 / BR-11：另一位教师、管理员、学生都不能审核这条申请。"""
    activity = review_required_activity(activity_factory, teacher)
    with app.app_context():
        assert services.enroll(activity.id, student.id)[0]

    url = f"/activities/{activity.id}/enrollments/{student.id}/approve"

    # 另一位教师 → 403
    login(other_teacher.student_no)
    assert client.post(url, data={"csrf_token": csrf_token("/dashboard")}).status_code == 403

    # 管理员 → 403（管理员不介入具体报名）
    login(admin.student_no)
    assert client.post(url, data={"csrf_token": csrf_token("/dashboard")}).status_code == 403

    # 学生 → 403
    login(student.student_no)
    assert client.post(url, data={"csrf_token": csrf_token("/dashboard")}).status_code == 403

    # 组织教师本人 → 302（成功重定向回名单页）
    login(teacher.student_no)
    assert client.post(url, data={"csrf_token": csrf_token("/dashboard")}).status_code == 302


def test_roster_page_shows_pending_and_review_buttons(app, client, teacher, student,
                                                      activity_factory, login, csrf_token):
    """教师端「名单与签到」页应有待审核区，通过按钮可用（FR-6.2）。"""
    activity = review_required_activity(activity_factory, teacher)
    with app.app_context():
        assert services.enroll(activity.id, student.id)[0]

    login(teacher.student_no)
    html = client.get(f"/activities/{activity.id}/enrollments").get_data(as_text=True)
    assert "待审核申请（1）" in html
    assert student.name in html

    resp = client.post(
        f"/activities/{activity.id}/enrollments/{student.id}/approve",
        data={"csrf_token": csrf_token("/dashboard")},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "该学生转为正式" in resp.get_data(as_text=True)


def test_rejected_student_sees_result_on_my_enrollments(
    app, client, teacher, student, activity_factory, login
):
    """FR-6.4：学生端能看到审核结论——「已拒绝」要在「我的报名」里说清楚。"""
    activity = review_required_activity(activity_factory, teacher)
    with app.app_context():
        assert services.enroll(activity.id, student.id)[0]
        services.review_enrollment(activity.id, student.id, teacher.id, approve=False)

    login(student.student_no)
    html = client.get("/my/enrollments").get_data(as_text=True)
    assert "已拒绝" in html
