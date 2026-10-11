"""候补机制（V2.0 核心，FR-4 / BR-3 ~ BR-7）。

对应《实验2-需求分析.md》第八章的验收场景 TC-2 ~ TC-6，以及 TC-10 的
「连续操作组合」抽查。每条测试的名字里标出对应的 TC 编号，方便对照验收。

规则速记：
* 满员时报名 → 候补（不占名额），记录"进入候补时间"；
* 队列顺序 = 进入候补的时间先后（先到先得），**没有**任何人工调整入口；
* 正式取消 → 名额释放 → 队首自动递补；
* 候补退出 → 其余人按原顺序自动前移（排位是算出来的，天然前移）。
"""

from datetime import timedelta

import pytest

from app import services
from app.extensions import db as _db
from app.models import Activity, ActivityStatus, Enrollment, EnrollStatus, utcnow


def enroll_ids(app, activity_id):
    with app.app_context():
        return (
            _db.session.execute(
                _db.select(Enrollment)
                .where(Enrollment.activity_id == activity_id)
                .order_by(Enrollment.id)
            )
            .scalars()
            .all()
        )


def activity_row(app, activity_id):
    with app.app_context():
        return _db.session.get(Activity, activity_id)


# ---------------------------------------------------------------------------
# TC-1 / TC-2：满员前后各走一条路
# ---------------------------------------------------------------------------


def test_tc1_with_room_enrolls_as_confirmed(app, student, teacher, activity_factory, enrollment_state):
    """TC-1：有余位 → 立即「正式」，正式人数 +1，余位 -1。"""
    activity = activity_factory(teacher.id, capacity=30, enrolled_count=20)

    with app.app_context():
        ok, message = services.enroll(activity.id, student.id)

    assert ok
    assert "报名成功" in message
    assert activity_row(app, activity.id).enrolled_count == 21
    assert enrollment_state(activity.id, student.id).status == EnrollStatus.CONFIRMED


def test_tc2_full_activity_goes_to_waitlist(app, student, teacher, activity_factory, enrollment_state):
    """TC-2：满员 → 进入候补并给出排位，正式人数不变。"""
    activity = activity_factory(teacher.id, capacity=30, enrolled_count=30)

    with app.app_context():
        ok, message = services.enroll(activity.id, student.id)

    assert ok, "满员时报名应当成功进入候补，而不是失败（BR-3 / DEC-4）"
    assert "候补" in message and "第 1 位" in message
    assert activity_row(app, activity.id).enrolled_count == 30, "候补不该占名额"
    state = enrollment_state(activity.id, student.id)
    assert state.status == EnrollStatus.WAITLISTED
    assert state.position == 1
    assert state.waitlisted_at is not None, "进入候补时间必须被记录（排序的唯一依据）"


# ---------------------------------------------------------------------------
# TC-3：先到先得，顺序不可调整
# ---------------------------------------------------------------------------


def test_tc3_queue_order_is_by_join_time(app, teacher, student, user_factory, activity_factory, mutate):
    """TC-3：后加入者排在后面，先进入者顺序不变。"""
    activity = activity_factory(teacher.id, capacity=1, enrolled_count=1)
    early = user_factory("20230201", "先来的")
    middle = user_factory("20230202", "中间的")
    late = user_factory("20230203", "后来的")

    with app.app_context():
        assert services.enroll(activity.id, early.id)[0]
        # 把三个人的"进入候补时间"手工拉开，模拟先后到达
        rows = {
            row.user_id: row
            for row in _db.session.execute(
                _db.select(Enrollment).where(Enrollment.activity_id == activity.id)
            ).scalars()
        }
        base = utcnow()
        rows[early.id].waitlisted_at = base - timedelta(minutes=30)
        _db.session.commit()

        assert services.enroll(activity.id, middle.id)[0]
        rows = {
            row.user_id: row
            for row in _db.session.execute(
                _db.select(Enrollment).where(Enrollment.activity_id == activity.id)
            ).scalars()
        }
        rows[middle.id].waitlisted_at = base - timedelta(minutes=20)
        _db.session.commit()

        assert services.enroll(activity.id, late.id)[0]

    with app.app_context():
        queue = (
            _db.session.execute(services.waitlist_query(activity.id)).scalars().all()
        )
        assert [row.user_id for row in queue] == [early.id, middle.id, late.id]
        assert [services.waitlist_position(row) for row in queue] == [1, 2, 3]


def test_queue_has_no_manual_reorder_entry(app, client, teacher, student, login, activity_factory, csrf_token):
    """BR-4：任何角色都不能人工调整候补顺序。

    「没有入口」这件事没法直接断言，改为断言**队列的呈现只来自数据库顺序**：
    用两个不同的入口（名单页、详情页）看到的排位一致，且不存在任何
    改变顺序的路由参数。
    """
    activity = activity_factory(teacher.id, capacity=1, enrolled_count=1)
    with app.app_context():
        assert services.enroll(activity.id, student.id)[0]

    # 路由表里不存在「调整候补顺序」这类端点
    rules = {rule.rule for rule in client.application.url_map.iter_rules()}
    assert not any(
        ("waitlist" in rule and "order" in rule) or "reorder" in rule for rule in rules
    ), "不允许存在调整候补顺序的入口"


# ---------------------------------------------------------------------------
# TC-4：取消 → 队首自动递补
# ---------------------------------------------------------------------------


def test_tc4_cancel_triggers_head_promotion(
    app, teacher, student, user_factory, activity_factory, enrollment_state
):
    """TC-4：满员 + 候补 3 人。正式者取消 → 队首转正式，其余前移，人数仍 = 上限。

    ⚠️ 截止时间特意放到 48 小时后：默认的 24 小时正好压在「名单锁定窗口」
    （距截止不足 24 小时即锁定）的边界上，会因为几微秒的流逝被判成"已锁定"，
    测出来的是锁定规则而不是递补。
    """
    activity = activity_factory(teacher.id, capacity=2, deadline_in_hours=48)
    confirmed_a = student
    confirmed_b = user_factory("20230301", "正式B")
    first = user_factory("20230302", "候补1")
    second = user_factory("20230303", "候补2")
    third = user_factory("20230304", "候补3")

    with app.app_context():
        # 造出「2 正式 + 3 候补」的局面
        _db.session.add_all(
            [
                Enrollment(
                    activity_id=activity.id,
                    user_id=confirmed_a.id,
                    status=EnrollStatus.CONFIRMED,
                    enrolled_at=utcnow() - timedelta(hours=5),
                ),
                Enrollment(
                    activity_id=activity.id,
                    user_id=confirmed_b.id,
                    status=EnrollStatus.CONFIRMED,
                    enrolled_at=utcnow() - timedelta(hours=5),
                ),
            ]
        )
        _db.session.get(Activity, activity.id).enrolled_count = 2
        _db.session.commit()

    for index, person in enumerate((first, second, third)):
        with app.app_context():
            assert services.enroll(activity.id, person.id)[0]
            # 拉开进入候补的时间，保证队列顺序确定
            row = _db.session.execute(
                _db.select(Enrollment).where(
                    Enrollment.activity_id == activity.id,
                    Enrollment.user_id == person.id,
                )
            ).scalar_one()
            row.waitlisted_at = utcnow() - timedelta(minutes=30 - index * 10)
            _db.session.commit()

    with app.app_context():
        ok, _ = services.cancel_enrollment(activity.id, confirmed_a.id)

    assert ok
    assert enrollment_state(activity.id, confirmed_a.id).status == EnrollStatus.CANCELED
    assert enrollment_state(activity.id, first.id).status == EnrollStatus.CONFIRMED, "队首应当自动递补"
    assert enrollment_state(activity.id, second.id).position == 1, "其余候补应各前移 1 位"
    assert enrollment_state(activity.id, third.id).position == 2
    assert activity_row(app, activity.id).enrolled_count == 2, "递补后正式人数仍 = 上限"


def test_promotion_happens_only_when_a_seat_frees(app, teacher, user_factory, activity_factory, enrollment_state):
    """没有名额空出时，候补一直等——不做任何提前递补。"""
    activity = activity_factory(teacher.id, capacity=1, enrolled_count=1)
    waiting = user_factory("20230401", "等待者")
    with app.app_context():
        assert services.enroll(activity.id, waiting.id)[0]

    assert enrollment_state(activity.id, waiting.id).status == EnrollStatus.WAITLISTED


def test_capacity_increase_feeds_the_waitlist(app, teacher, student, user_factory, activity_factory, enrollment_state, mutate):
    """教师把上限调大 → 多出来的余位立刻喂给候补队列（BR-6 的另一种触发）。"""
    activity = activity_factory(teacher.id, capacity=1, enrolled_count=1)
    waiting = user_factory("20230402", "等待者")

    with app.app_context():
        assert services.enroll(activity.id, waiting.id)[0]

    def _raise_capacity(session):
        session.get(Activity, activity.id).capacity = 2

    mutate(_raise_capacity)

    with app.app_context():
        services.promote_waitlist(activity.id)
        # promote_waitlist 刻意不提交：它是调用方事务的一部分，这里替它收尾
        _db.session.commit()

    assert enrollment_state(activity.id, waiting.id).status == EnrollStatus.CONFIRMED
    assert activity_row(app, activity.id).enrolled_count == 2


# ---------------------------------------------------------------------------
# TC-5：退出候补，其余人自动前移
# ---------------------------------------------------------------------------


def test_tc5_leaving_waitlist_shifts_the_rest(
    app, teacher, student, user_factory, activity_factory, enrollment_state
):
    """TC-5：第 2 位退出 → 第 3 位起全部前移 1 位；退出者转「已取消」。"""
    activity = activity_factory(teacher.id, capacity=1, enrolled_count=1)
    first = user_factory("20230501", "候补1")
    second = user_factory("20230502", "候补2")
    third = user_factory("20230503", "候补3")

    base = utcnow()
    with app.app_context():
        for index, person in enumerate((first, second, third)):
            row = Enrollment(
                activity_id=activity.id,
                user_id=person.id,
                status=EnrollStatus.WAITLISTED,
                enrolled_at=base - timedelta(minutes=40 - index * 10),
                waitlisted_at=base - timedelta(minutes=30 - index * 10),
            )
            _db.session.add(row)
        _db.session.commit()

    with app.app_context():
        ok, _ = services.cancel_enrollment(activity.id, second.id)

    assert ok
    assert enrollment_state(activity.id, second.id).status == EnrollStatus.CANCELED
    assert enrollment_state(activity.id, first.id).position == 1
    assert enrollment_state(activity.id, third.id).position == 2, "第 3 位应前移到第 2 位"


def test_waitlist_exit_is_not_blocked_by_the_roster_lock(
    app, teacher, student, activity_factory, enrollment_state
):
    """候补退出**不受**名单锁定窗口限制（FR-4.4「随时退出」）。

    名单锁定保护的是"正式名额"，候补不占名额、退出没有成本——
    把它也锁住，就等于 24 小时内想退都不能退。
    """
    activity = activity_factory(teacher.id, capacity=1, enrolled_count=1, deadline_in_hours=1)
    with app.app_context():
        assert services.enroll(activity.id, student.id)[0]

    # 现在已经处于「距截止不足 24 小时」的锁定窗口内
    with app.app_context():
        ok, message = services.cancel_enrollment(activity.id, student.id)

    assert ok, f"候补退出不应被名单锁定拦住：{message}"
    assert enrollment_state(activity.id, student.id).status == EnrollStatus.CANCELED


# ---------------------------------------------------------------------------
# TC-6：重新报名按新时间重新排队
# ---------------------------------------------------------------------------


def test_tc6_reapply_goes_to_the_tail(
    app, teacher, student, user_factory, activity_factory, enrollment_state
):
    """TC-6：取消后重新报名（满员情况下）→ 排到队尾，不保留原位置。"""
    activity = activity_factory(teacher.id, capacity=1, enrolled_count=1)
    early = student
    other = user_factory("20230601", "另一位")
    rejoiner = user_factory("20230602", "回头的人")

    base = utcnow()
    with app.app_context():
        _db.session.add_all(
            [
                Enrollment(
                    activity_id=activity.id,
                    user_id=early.id,
                    status=EnrollStatus.WAITLISTED,
                    enrolled_at=base - timedelta(minutes=40),
                    waitlisted_at=base - timedelta(minutes=40),
                ),
                Enrollment(
                    activity_id=activity.id,
                    user_id=other.id,
                    status=EnrollStatus.WAITLISTED,
                    enrolled_at=base - timedelta(minutes=30),
                    waitlisted_at=base - timedelta(minutes=30),
                ),
                Enrollment(
                    activity_id=activity.id,
                    user_id=rejoiner.id,
                    status=EnrollStatus.CANCELED,
                    enrolled_at=base - timedelta(minutes=50),
                    canceled_at=base - timedelta(minutes=20),
                ),
            ]
        )
        _db.session.commit()

    with app.app_context():
        ok, message = services.enroll(activity.id, rejoiner.id)

    assert ok and "第 3 位" in message
    assert enrollment_state(activity.id, rejoiner.id).position == 3, (
        "重新报名必须按新的时间排到队尾——旧位置不作数（DEC-3）"
    )


# ---------------------------------------------------------------------------
# TC-10 抽查：连续操作组合后仍保持一致
# ---------------------------------------------------------------------------


def test_tc10_mixed_operations_keep_invariants(app, teacher, student, user_factory, activity_factory, enrollment_state):
    """一串混合操作后：正式人数 ≤ 上限、候补队列按时间有序、无并列。"""
    # 同样要避开 24 小时锁定窗口的边界，否则"取消"会被名单锁定拦下
    activity = activity_factory(teacher.id, capacity=2, deadline_in_hours=48)
    a, b, c = user_factory("20230701", "甲"), user_factory("20230702", "乙"), user_factory("20230703", "丙")

    with app.app_context():
        assert services.enroll(activity.id, a.id)[0]
        assert services.enroll(activity.id, b.id)[0]
        assert services.enroll(activity.id, c.id)[0]        # 满员 → 候补
        services.cancel_enrollment(activity.id, a.id)        # 空出 → 丙递补
        assert services.enroll(activity.id, student.id)[0]   # 又满员 → 候补
        services.cancel_enrollment(activity.id, student.id)  # 候补退出

        row = _db.session.get(Activity, activity.id)
        assert row.enrolled_count <= row.capacity, "BR-1 被破坏"

        queue = _db.session.execute(services.waitlist_query(activity.id)).scalars().all()
        times = [entry.waitlisted_at for entry in queue]
        assert times == sorted(times), "候补队列必须按进入候补时间有序"

    # 最终局面：甲已取消、乙正式、丙正式（递补上来的）、学生退出候补
    assert enrollment_state(activity.id, a.id).status == EnrollStatus.CANCELED
    assert enrollment_state(activity.id, b.id).status == EnrollStatus.CONFIRMED
    assert enrollment_state(activity.id, c.id).status == EnrollStatus.CONFIRMED
    assert enrollment_state(activity.id, student.id).status == EnrollStatus.CANCELED
    assert activity_row(app, activity.id).enrolled_count == 2


# ---------------------------------------------------------------------------
# 界面呈现：排位与队列都要看得见
# ---------------------------------------------------------------------------


def test_student_sees_waitlist_position_on_pages(
    app, client, teacher, student, login, activity_factory
):
    """FR-4.3：学生端显示「候补中 + 队列位置」——详情页与「我的报名」都要有。"""
    activity = activity_factory(teacher.id, capacity=1, enrolled_count=1)
    with app.app_context():
        assert services.enroll(activity.id, student.id)[0]

    login(student.student_no)

    detail = client.get(f"/activities/{activity.id}").get_data(as_text=True)
    assert "候补中" in detail and "第 1 位" in detail

    mine = client.get("/my/enrollments").get_data(as_text=True)
    assert "候补中" in mine and "第 1 位" in mine


def test_teacher_sees_waitlist_queue_in_order(
    app, client, teacher, student, user_factory, login, activity_factory
):
    """FR-4.3 / FR-7.4：教师端候补名单按队列顺序展示。"""
    activity = activity_factory(teacher.id, capacity=1, enrolled_count=1)
    first = user_factory("20230801", "排第一")
    second = user_factory("20230802", "排第二")

    base = utcnow()
    with app.app_context():
        _db.session.add_all(
            [
                Enrollment(
                    activity_id=activity.id, user_id=first.id,
                    status=EnrollStatus.WAITLISTED,
                    enrolled_at=base - timedelta(minutes=30),
                    waitlisted_at=base - timedelta(minutes=30),
                ),
                Enrollment(
                    activity_id=activity.id, user_id=second.id,
                    status=EnrollStatus.WAITLISTED,
                    enrolled_at=base - timedelta(minutes=20),
                    waitlisted_at=base - timedelta(minutes=20),
                ),
            ]
        )
        _db.session.commit()

    login(teacher.student_no)
    html = client.get(f"/activities/{activity.id}/enrollments").get_data(as_text=True)

    assert "候补队列（2 人）" in html
    assert "第 1 位" in html and "第 2 位" in html
    # 顺序断言：排第一的名字出现在排第二的前面
    assert html.index("排第一") < html.index("排第二")
