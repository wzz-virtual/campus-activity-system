"""报名与取消报名（V2.0），含并发控制与 24 小时名单锁定。

这是整个系统最容易翻车的地方，所以这里的测试分三层：

1. **规则层** —— 直接调 ``services.enroll``，能把时间卡到「截止前后各一秒」；
2. **接口层** —— 走真实 HTTP，验证按钮状态与拦截提示；
3. **并发层** —— 多线程同时报名，断言不超卖、不重复。

候补队列与自动递补的专项测试见 ``test_waitlist.py``；
报名审核（"通过 ≠ 正式"）见 ``test_review.py``。
"""

import threading
from datetime import timedelta

import pytest

from app import services
from app.extensions import db as _db
from app.models import (
    Activity,
    ActivityStatus,
    Enrollment,
    EnrollStatus,
    Role,
    User,
    utcnow,
)
from app.services import CANCEL_LOCK_WINDOW


def post(client, csrf_token, path, follow_redirects=False, **data):
    payload = {"csrf_token": csrf_token("/dashboard")}
    payload.update(data)
    return client.post(path, data=payload, follow_redirects=follow_redirects)


def enroll_rows(app, activity_id):
    with app.app_context():
        return (
            _db.session.execute(
                _db.select(Enrollment).where(Enrollment.activity_id == activity_id)
            )
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# 报名的四项检查
# ---------------------------------------------------------------------------


def test_happy_path_enroll(client, login, csrf_token, student, teacher, activity_factory, activity_state):
    activity = activity_factory(teacher.id, capacity=10)
    login(student.student_no)

    resp = post(client, csrf_token, f"/activities/{activity.id}/enroll")
    assert resp.status_code == 302

    state = activity_state(activity.id)
    assert state.enrolled_count == 1
    rows = enroll_rows(client.application, activity.id)
    assert len(rows) == 1 and rows[0].status == EnrollStatus.CONFIRMED


@pytest.mark.parametrize(
    "status,expected",
    [
        (ActivityStatus.DRAFT, "该活动尚未发布"),
        (ActivityStatus.ONGOING, "活动已开始，报名通道已关闭"),
        (ActivityStatus.FINISHED, "该活动已结束"),
        (ActivityStatus.CANCELED, "该活动已取消"),
        (ActivityStatus.OFFLINE, "该活动已被管理员下线，暂不接受报名"),
    ],
)
def test_enroll_requires_published_status(
    app, student, teacher, activity_factory, status, expected
):
    """只有「已发布」的活动可以报名。

    直接调规则层，避免 HTTP 层的状态同步把活动提前推进到 ONGOING，
    反而测不到我们想测的那个分支。

    ⚠️ 注意「满员」**不在**被拒之列：那是候补的入口，不是失败（BR-3）。
    """
    activity = activity_factory(teacher.id, status=status, start_in_hours=48)
    with app.app_context():
        ok, message = services.enroll(activity.id, student.id)

    assert not ok
    assert message == expected


def test_enroll_rejects_duplicate(client, login, csrf_token, student, teacher, activity_factory, activity_state):
    """重复报名不改变人数，也不该多出一条记录。"""
    activity = activity_factory(teacher.id, capacity=10)
    login(student.student_no)

    post(client, csrf_token, f"/activities/{activity.id}/enroll")
    resp = post(client, csrf_token, f"/activities/{activity.id}/enroll", follow_redirects=True)

    assert activity_state(activity.id).enrolled_count == 1
    assert len(enroll_rows(client.application, activity.id)) == 1
    assert "你已报名成功" in resp.get_data(as_text=True)
    assert "无需重复提交" in resp.get_data(as_text=True)


def test_enroll_requires_login(client, csrf_token, student, teacher, activity_factory):
    """未登录不能报名：带上合法的 CSRF 凭证也只是被送到登录页。"""
    activity = activity_factory(teacher.id)
    resp = client.post(
        f"/activities/{activity.id}/enroll", data={"csrf_token": csrf_token()}
    )
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_enroll_without_csrf_token_is_rejected(client, student, teacher, activity_factory):
    """没有 CSRF 凭证的写请求在进入视图之前就被拦掉。"""
    activity = activity_factory(teacher.id)
    assert client.post(f"/activities/{activity.id}/enroll").status_code == 400


# ---------------------------------------------------------------------------
# 时间边界：截止前后各一秒
# ---------------------------------------------------------------------------


def test_enroll_deadline_boundary_is_exact_to_the_second(app, student, teacher, activity_factory):
    """报名截止前后各一秒的行为。

    在同一个应用上下文里「先设截止时间、立刻报名」，中间只隔几微秒，
    才能真的把边界卡到秒级；走 HTTP 的话光是请求开销就有几十毫秒，
    ±1 秒这种断言根本立不住。
    """
    # 截止还差 1 秒 —— 可以报名
    just_in_time = activity_factory(teacher.id, deadline_in_hours=1)
    with app.app_context():
        activity = _db.session.get(Activity, just_in_time.id)
        activity.enroll_deadline = utcnow() + timedelta(seconds=1)
        _db.session.commit()
        ok, _ = services.enroll(just_in_time.id, student.id)
    assert ok, "截止前 1 秒应该还能报名"

    # 截止已过 1 秒 —— 拒绝
    just_late = activity_factory(teacher.id, deadline_in_hours=1)
    with app.app_context():
        activity = _db.session.get(Activity, just_late.id)
        activity.enroll_deadline = utcnow() - timedelta(seconds=1)
        _db.session.commit()
        ok, message = services.enroll(just_late.id, student.id)
    assert not ok and message == "报名已截止", "截止后 1 秒必须被拒绝"


# ---------------------------------------------------------------------------
# 并发：不超卖、不重复
# ---------------------------------------------------------------------------


def test_concurrent_enroll_never_exceeds_capacity(concurrent_app):
    """并发压测：40 个人同时抢 25 个名额。

    V2.0 的新语义：抢不到的**不再被丢弃**，而是全部进入候补队列（BR-3）。
    断言四件事：

    * ``enrolled_count`` 恰好等于人数上限，不多不少（BR-1）；
    * 正式记录数等于上限、候补记录数等于剩下的 15，且没有重复的 ``(活动, 用户)``；
    * 候补排位是 1..15 的一次排列——同一微秒写入也不会出现并列或跳号；
    * 成功返回的次数 = 正式 + 候补——没有"被丢弃"的报名。
    """
    user_count = 40
    capacity = 25

    with concurrent_app.app_context():
        from app.models import Category

        category = Category(name="并发测试", sort_order=0)
        organizer = User(student_no="org-concurrent", name="组织者", role=Role.TEACHER)
        organizer.set_password("Test-Password-2026")
        _db.session.add_all([category, organizer])
        _db.session.flush()

        now = utcnow()
        activity = Activity(
            title="并发报名压测",
            category_id=category.id,
            organizer_id=organizer.id,
            start_time=now + timedelta(hours=48),
            end_time=now + timedelta(hours=50),
            enroll_deadline=now + timedelta(hours=24),
            location="线上",
            capacity=capacity,
            enrolled_count=0,
            status=ActivityStatus.PUBLISHED,
        )
        _db.session.add(activity)
        _db.session.flush()

        user_ids = []
        for index in range(user_count):
            person = User(
                student_no=f"concurrent-{index:03d}",
                name=f"学生{index}",
                role=Role.STUDENT,
            )
            person.set_password("Test-Password-2026")
            _db.session.add(person)
            _db.session.flush()
            user_ids.append(person.id)

        _db.session.commit()
        activity_id = activity.id

    # barrier 让所有线程尽量在同一瞬间发起报名，把竞态窗口撑到最大
    barrier = threading.Barrier(user_count)
    results = []
    errors = []

    def worker(user_id):
        try:
            with concurrent_app.app_context():
                barrier.wait(timeout=30)
                ok, message = services.enroll(activity_id, user_id)
                results.append((user_id, ok, message))
        except Exception as exc:  # noqa: BLE001 - 线程里的异常要带回主线程才能看见
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(uid,)) for uid in user_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"并发过程中抛出异常：{errors}"

    with concurrent_app.app_context():
        activity = _db.session.get(Activity, activity_id)
        rows = (
            _db.session.execute(
                _db.select(Enrollment).where(Enrollment.activity_id == activity_id)
            )
            .scalars()
            .all()
        )

        confirmed = [row for row in rows if row.status == EnrollStatus.CONFIRMED]
        waitlisted = [row for row in rows if row.status == EnrollStatus.WAITLISTED]

        assert activity.enrolled_count == capacity, (
            f"超卖或被少扣：enrolled_count={activity.enrolled_count}，上限={capacity}"
        )
        assert len(rows) == user_count, "有人被丢弃了——抢不到名额应当是进入候补"
        assert len(confirmed) == capacity
        assert len(waitlisted) == user_count - capacity
        assert len({row.user_id for row in rows}) == user_count, "出现了重复报名记录"

        # 候补排位必须是不重不漏的 1..N
        positions = sorted(
            services.waitlist_position(row) for row in waitlisted
        )
        assert positions == list(range(1, user_count - capacity + 1))

    succeeded = [item for item in results if item[1]]
    assert len(succeeded) == user_count, "成功次数与落库记录数对不上"


def test_concurrent_double_submit_by_same_user_creates_one_record(concurrent_app):
    """同一个人并发点两次报名：唯一索引兜底，只留一条记录、只扣一个名额。"""
    with concurrent_app.app_context():
        from app.models import Category

        category = Category(name="重复提交测试", sort_order=0)
        organizer = User(student_no="org-dup", name="组织者", role=Role.TEACHER)
        organizer.set_password("Test-Password-2026")
        _db.session.add_all([category, organizer])
        _db.session.flush()

        now = utcnow()
        activity = Activity(
            title="重复提交压测",
            category_id=category.id,
            organizer_id=organizer.id,
            start_time=now + timedelta(hours=48),
            end_time=now + timedelta(hours=50),
            enroll_deadline=now + timedelta(hours=24),
            location="线上",
            capacity=10,
            enrolled_count=0,
            status=ActivityStatus.PUBLISHED,
        )
        student = User(student_no="dup-user", name="手快的学生", role=Role.STUDENT)
        student.set_password("Test-Password-2026")
        _db.session.add_all([activity, student])
        _db.session.commit()
        activity_id, student_id = activity.id, student.id

    barrier = threading.Barrier(2)
    results = []

    def worker():
        with concurrent_app.app_context():
            barrier.wait(timeout=30)
            results.append(services.enroll(activity_id, student_id))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    with concurrent_app.app_context():
        activity = _db.session.get(Activity, activity_id)
        rows = (
            _db.session.execute(
                _db.select(Enrollment).where(Enrollment.activity_id == activity_id)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert activity.enrolled_count == 1, "重复提交把名额多扣了"
        assert sum(1 for ok, _ in results if ok) == 1


# ---------------------------------------------------------------------------
# 取消报名与名单锁定
# ---------------------------------------------------------------------------


def test_cancel_enrollment_releases_the_seat(client, login, csrf_token, student, teacher, activity_factory, activity_state):
    # 截止时间刻意放到 48 小时后：锁定窗口是「距截止不足 24 小时」，
    # 用默认的 24 小时会正好落在窗口边界上，测出来的是锁定规则而不是释放名额
    activity = activity_factory(teacher.id, capacity=10, deadline_in_hours=48)
    login(student.student_no)

    post(client, csrf_token, f"/activities/{activity.id}/enroll")
    assert activity_state(activity.id).enrolled_count == 1

    post(client, csrf_token, f"/activities/{activity.id}/unenroll")
    assert activity_state(activity.id).enrolled_count == 0

    rows = enroll_rows(client.application, activity.id)
    assert rows[0].status == EnrollStatus.CANCELED


def test_canceled_enrollment_can_be_revived(client, login, csrf_token, student, teacher, activity_factory, activity_state):
    """取消之后还能再报回来——但复用的是同一条记录，不会多出一条。"""
    activity = activity_factory(teacher.id, capacity=10, deadline_in_hours=48)
    login(student.student_no)

    post(client, csrf_token, f"/activities/{activity.id}/enroll")
    post(client, csrf_token, f"/activities/{activity.id}/unenroll")
    post(client, csrf_token, f"/activities/{activity.id}/enroll")

    assert activity_state(activity.id).enrolled_count == 1
    rows = enroll_rows(client.application, activity.id)
    assert len(rows) == 1 and rows[0].status == EnrollStatus.CONFIRMED


def test_cancel_is_blocked_inside_the_24_hour_lock_window(
    app, student, teacher, activity_factory
):
    """距报名截止不足 24 小时起，名单锁定。"""
    activity = activity_factory(teacher.id, deadline_in_hours=1)
    with app.app_context():
        assert services.enroll(activity.id, student.id)[0]

        current = _db.session.get(Activity, activity.id)
        # 正好卡在锁定窗口边缘之内（差 1 分钟满 24 小时）
        current.enroll_deadline = utcnow() + CANCEL_LOCK_WINDOW - timedelta(minutes=1)
        _db.session.commit()

        ok, message = services.cancel_enrollment(activity.id, student.id)

    assert not ok
    assert message == "距报名截止不足 24 小时，名单已锁定"


def test_cancel_is_allowed_just_outside_the_lock_window(app, student, teacher, activity_factory):
    activity = activity_factory(teacher.id, deadline_in_hours=48)
    with app.app_context():
        assert services.enroll(activity.id, student.id)[0]

        current = _db.session.get(Activity, activity.id)
        # 距截止还有 24 小时零 1 分钟，还能反悔
        current.enroll_deadline = utcnow() + CANCEL_LOCK_WINDOW + timedelta(minutes=1)
        _db.session.commit()

        ok, message = services.cancel_enrollment(activity.id, student.id)

    assert ok, message


@pytest.mark.parametrize(
    "status,expected",
    [
        (ActivityStatus.FINISHED, "活动已结束"),
        (ActivityStatus.CANCELED, "活动已取消"),
        (ActivityStatus.ONGOING, "活动已开始，名单已锁定"),
        (ActivityStatus.OFFLINE, "该活动已被管理员下线，暂不能操作"),
        (ActivityStatus.DRAFT, "该活动尚未发布"),
    ],
)
def test_cancel_blocked_by_activity_status(
    app, student, teacher, activity_factory, mutate, status, expected
):
    activity = activity_factory(teacher.id, deadline_in_hours=48)
    with app.app_context():
        assert services.enroll(activity.id, student.id)[0]

    mutate(lambda s: setattr(s.get(Activity, activity.id), "status", status))

    with app.app_context():
        ok, message = services.cancel_enrollment(activity.id, student.id)

    assert not ok
    assert message == expected


def test_checked_in_enrollment_cannot_be_canceled(app, student, teacher, activity_factory, mutate):
    """已签到的报名不可取消，否则统计口径会前后矛盾。"""
    activity = activity_factory(teacher.id, deadline_in_hours=48)
    with app.app_context():
        assert services.enroll(activity.id, student.id)[0]

    def _check_in(session):
        row = session.execute(
            _db.select(Enrollment).where(Enrollment.activity_id == activity.id)
        ).scalar_one()
        row.check_in_at = utcnow()

    mutate(_check_in)

    with app.app_context():
        ok, message = services.cancel_enrollment(activity.id, student.id)

    assert not ok
    assert message == "你已签到，无法取消报名"


def test_cancel_deadline_text_is_shown_while_still_cancelable(
    client, login, csrf_token, student, teacher, activity_factory
):
    """还能取消的时候就要写明可以反悔到什么时候，而不是等锁定了才说不行。"""
    activity = activity_factory(teacher.id, deadline_in_hours=48)
    login(student.student_no)

    post(client, csrf_token, f"/activities/{activity.id}/enroll")

    detail = client.get(f"/activities/{activity.id}").get_data(as_text=True)
    assert "可以取消报名" in detail
    assert "名单锁定" in detail


def test_repeated_cancel_is_rejected(client, login, csrf_token, student, teacher, activity_factory):
    activity = activity_factory(teacher.id, deadline_in_hours=48)
    login(student.student_no)

    post(client, csrf_token, f"/activities/{activity.id}/enroll")
    post(client, csrf_token, f"/activities/{activity.id}/unenroll")
    resp = post(client, csrf_token, f"/activities/{activity.id}/unenroll", follow_redirects=True)

    assert "你还没有报名这个活动" in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# 我的报名
# ---------------------------------------------------------------------------


def test_my_enrollments_shows_four_check_in_states(
    client, login, student, teacher, activity_factory, enroll_factory
):
    """签到状态对「正式」报名分四种呈现：待参加 / 待签到 / 已签到 / 缺席。"""
    upcoming = activity_factory(teacher.id, title="待参加的活动")
    ongoing = activity_factory(teacher.id, title="待签到的活动", status=ActivityStatus.ONGOING)
    checked = activity_factory(teacher.id, title="已签到的活动", status=ActivityStatus.FINISHED)
    absent = activity_factory(teacher.id, title="缺席的活动", status=ActivityStatus.FINISHED)

    enroll_factory(upcoming.id, [student.id])
    enroll_factory(ongoing.id, [student.id])
    enroll_factory(checked.id, [student.id], checked_in=[student.id])
    enroll_factory(absent.id, [student.id])

    login(student.student_no)
    html = client.get("/my/enrollments").get_data(as_text=True)

    assert "待参加" in html
    assert "待签到" in html
    assert "已签到" in html
    assert "缺席" in html


def test_my_enrollments_explains_why_cancel_is_unavailable(
    client, login, student, teacher, activity_factory, enroll_factory
):
    """不可取消时必须逐条给出原因，不能只有一个置灰的按钮。"""
    locked = activity_factory(teacher.id, title="锁定的活动", deadline_in_hours=1)
    enroll_factory(locked.id, [student.id])

    login(student.student_no)
    html = client.get("/my/enrollments").get_data(as_text=True)

    assert "距报名截止不足 24 小时，名单已锁定" in html
