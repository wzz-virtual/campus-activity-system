"""M2：七态状态机、审核流、审核日志与权限拦截。

重点是把「非法流转必须被拒绝」测到：状态机的价值不在允许什么，
而在于**不允许什么**——少拦一条，数据就会出现在不该出现的状态里。
"""

import pytest

from app.models import ActivityStatus, AuditAction, AuditLog
from app.extensions import db as _db


def post(client, csrf_token, path, **data):
    payload = {"csrf_token": csrf_token("/dashboard")}
    payload.update(data)
    return client.post(path, data=payload)


def audit_actions(app, activity_id):
    """读某个活动的审核日志动作序列。"""
    with app.app_context():
        logs = (
            _db.session.execute(
                _db.select(AuditLog)
                .where(AuditLog.activity_id == activity_id)
                .order_by(AuditLog.created_at, AuditLog.id)
            )
            .scalars()
            .all()
        )
        return [log.action for log in logs]


# ---------------------------------------------------------------------------
# 正常流转
# ---------------------------------------------------------------------------


def test_full_review_round_trip(
    client, login, csrf_token, club, admin, activity_factory, activity_state, app
):
    """草稿 → 待审核 → 已发布 → 学生可见。"""
    activity = activity_factory(club.id, title="人像摄影体验日", status=ActivityStatus.DRAFT)

    login(club.student_no)
    post(client, csrf_token, f"/activities/{activity.id}/submit")
    assert activity_state(activity.id).status == ActivityStatus.PENDING

    login(admin.student_no)
    post(client, csrf_token, f"/activities/{activity.id}/approve")
    state = activity_state(activity.id)

    assert state.status == ActivityStatus.PUBLISHED
    assert state.approved_by == admin.id
    assert audit_actions(app, activity.id) == [AuditAction.SUBMIT, AuditAction.APPROVE]

    # 通过之后才对学生可见
    plaza = client.get("/activities").get_data(as_text=True)
    assert "人像摄影体验日" in plaza


def test_reject_requires_reason_and_shows_it_to_creator(
    client, login, csrf_token, club, admin, activity_factory, activity_state
):
    activity = activity_factory(club.id, title="电竞友谊赛", status=ActivityStatus.PENDING)

    login(admin.student_no)
    # 不填原因：必须被拒绝，而且状态不能变
    post(client, csrf_token, f"/activities/{activity.id}/reject", reason="   ")
    assert activity_state(activity.id).status == ActivityStatus.PENDING

    post(
        client,
        csrf_token,
        f"/activities/{activity.id}/reject",
        reason="活动时间与校运会冲突，请调整后重新提交",
    )
    state = activity_state(activity.id)
    assert state.status == ActivityStatus.REJECTED
    assert state.reject_reason == "活动时间与校运会冲突，请调整后重新提交"

    # 创建者必须能看到原因，否则他不知道要改什么
    login(club.student_no)
    detail = client.get(f"/activities/{activity.id}").get_data(as_text=True)
    assert "活动时间与校运会冲突" in detail


def test_rejected_activity_can_be_resubmitted(
    client, login, csrf_token, club, activity_factory, activity_state, app
):
    activity = activity_factory(
        club.id, status=ActivityStatus.REJECTED, reject_reason="时间冲突"
    )
    login(club.student_no)

    post(client, csrf_token, f"/activities/{activity.id}/submit")
    assert activity_state(activity.id).status == ActivityStatus.PENDING
    assert audit_actions(app, activity.id) == [AuditAction.SUBMIT]


def test_creator_can_withdraw_pending_activity(
    client, login, csrf_token, club, activity_factory, activity_state
):
    activity = activity_factory(club.id, status=ActivityStatus.PENDING)
    login(club.student_no)

    post(client, csrf_token, f"/activities/{activity.id}/withdraw")
    assert activity_state(activity.id).status == ActivityStatus.DRAFT


def test_activity_can_be_canceled_before_it_ends(
    client, login, csrf_token, club, activity_factory, activity_state
):
    activity = activity_factory(club.id, status=ActivityStatus.PUBLISHED)
    login(club.student_no)

    post(client, csrf_token, f"/activities/{activity.id}/cancel", reason="场地临时被占用")
    assert activity_state(activity.id).status == ActivityStatus.CANCELED


# ---------------------------------------------------------------------------
# 非法流转必须被拒绝
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start_status,path,expected_status",
    [
        # 已发布的活动不能再"提交审核"
        (ActivityStatus.PUBLISHED, "submit", ActivityStatus.PUBLISHED),
        # 已取消的活动不能提交
        (ActivityStatus.CANCELED, "submit", ActivityStatus.CANCELED),
        # 草稿不能直接"通过"（必须先提交）
        (ActivityStatus.DRAFT, "approve", ActivityStatus.DRAFT),
        # 已发布的活动不能再次审核通过
        (ActivityStatus.PUBLISHED, "approve", ActivityStatus.PUBLISHED),
        # 草稿没有被驳回，谈不上"撤回"
        (ActivityStatus.DRAFT, "withdraw", ActivityStatus.DRAFT),
        # 已结束的活动不能取消
        (ActivityStatus.FINISHED, "cancel", ActivityStatus.FINISHED),
        # 已取消的活动不能再取消
        (ActivityStatus.CANCELED, "cancel", ActivityStatus.CANCELED),
    ],
)
def test_illegal_transitions_are_rejected(
    client, login, csrf_token, club, activity_factory, activity_state,
    start_status, path, expected_status,
):
    activity = activity_factory(club.id, status=start_status)
    login(club.student_no)

    post(client, csrf_token, f"/activities/{activity.id}/{path}", reason="随便给个原因")
    assert activity_state(activity.id).status == expected_status


def test_published_activity_cannot_be_approved_twice(
    client, login, csrf_token, admin, activity_factory, activity_state, app
):
    """重复审核只应留下一条 APPROVE 日志。"""
    activity = activity_factory(admin.id, status=ActivityStatus.PENDING)
    login(admin.student_no)

    post(client, csrf_token, f"/activities/{activity.id}/approve")
    post(client, csrf_token, f"/activities/{activity.id}/approve")

    assert activity_state(activity.id).status == ActivityStatus.PUBLISHED
    assert audit_actions(app, activity.id).count(AuditAction.APPROVE) == 1


# ---------------------------------------------------------------------------
# 审核权限
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role_fixture", ["student", "club"])
def test_non_admin_cannot_approve_or_reject(
    client, login, csrf_token, request, role_fixture, activity_factory, activity_state
):
    """审核通过 / 驳回只能由校级管理员做。"""
    actor = request.getfixturevalue(role_fixture)
    activity = activity_factory(actor.id, status=ActivityStatus.PENDING)
    login(actor.student_no)

    assert post(client, csrf_token, f"/activities/{activity.id}/approve").status_code == 403
    assert post(client, csrf_token, f"/activities/{activity.id}/reject", reason="x").status_code == 403
    assert activity_state(activity.id).status == ActivityStatus.PENDING


def test_club_cannot_cancel_another_clubs_activity(
    client, login, csrf_token, club, user_factory, activity_factory, activity_state
):
    stranger = user_factory("club009", "棋类社", "CLUB")
    activity = activity_factory(club.id, status=ActivityStatus.PUBLISHED)

    login(stranger.student_no)
    resp = post(client, csrf_token, f"/activities/{activity.id}/cancel")

    assert resp.status_code == 403
    assert activity_state(activity.id).status == ActivityStatus.PUBLISHED


def test_admin_can_cancel_any_activity(
    client, login, csrf_token, admin, club, activity_factory, activity_state
):
    """管理员对全部活动有取消权。"""
    activity = activity_factory(club.id, status=ActivityStatus.PUBLISHED)

    login(admin.student_no)
    post(client, csrf_token, f"/activities/{activity.id}/cancel")
    assert activity_state(activity.id).status == ActivityStatus.CANCELED


def test_student_cannot_see_pending_queue(client, login, student):
    """审核中心只有管理员能进。"""
    login(student.student_no)
    assert client.get("/admin/audit").status_code == 403


def test_audit_log_is_append_only_and_visible_to_admin(
    client, login, csrf_token, club, admin, activity_factory
):
    """审核日志只追加、不修改；管理员能翻到谁在什么时候批了什么。"""
    activity = activity_factory(club.id, title="书法体验课", status=ActivityStatus.PENDING)

    login(admin.student_no)
    post(
        client,
        csrf_token,
        f"/activities/{activity.id}/reject",
        reason="场地未报备，请补办手续",
    )

    audit = client.get("/admin/audit").get_data(as_text=True)
    assert "书法体验课" in audit
    assert "审核驳回" in audit
    assert "场地未报备，请补办手续" in audit
    assert "陈老师" in audit  # 操作人


def test_admin_direct_publish_skips_review_queue(
    client, login, csrf_token, admin, category, activity_factory, activity_state
):
    """管理员创建的活动不经过 PENDING，也就不该出现在待审核队列里。"""
    login(admin.student_no)
    resp = post(
        client,
        csrf_token,
        "/activities",
        title="校级讲座",
        category_id=str(category.id),
        description="",
        start_time="2099-01-01T10:00",
        end_time="2099-01-01T12:00",
        enroll_deadline="2098-12-31T10:00",
        location="大礼堂",
        capacity="200",
        action="submit",
    )
    activity_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    assert activity_state(activity_id).status == ActivityStatus.PUBLISHED

    login(admin.student_no)
    assert "校级讲座" not in client.get("/admin/audit").get_data(as_text=True)
