"""V3.0 数据驱动需求（DR-1 ~ DR-12）的自动化验收。

出处：《实验3-需求分析.md》。这里只覆盖**能在本系统里客观验证**的部分：

* DR-1 满员统一候补入口（含并发压测：0 次裸错误）
* DR-2 提交回执与防重复
* DR-3 状态变更主动通知
* DR-4 候补透明度（位次之外给转正率与机会标签）
* DR-5 指标采集（管理端看板的数据源）
* DR-6 递补实时化（取消与递补同事务，0 延迟）
* DR-7 搜索与筛选（日期 / 周末 / 报名状态）
* DR-8 服务端错误兜底页
* DR-10 结构化地点
* DR-11 时间冲突二次确认
* DR-12 热度标签与容量参考

DR-9（移动端优先）是界面走查项，由浏览器自动化验收覆盖，不在这里做接口断言。
"""

import threading
from datetime import timedelta

from app import metrics, services
from app.extensions import db as _db
from app.models import (
    Activity,
    ActivityStatus,
    Category,
    EnrollStatus,
    Enrollment,
    Message,
    MessageRecipient,
    Role,
    User,
    utcnow,
)
from app.services import (
    activity_heat,
    capacity_advice,
    waitlist_opportunity,
)
from app.timeutils import parse_local, to_local


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def enroll_post(client, csrf_token, activity_id, data=None, follow_redirects=True):
    payload = {"csrf_token": csrf_token(f"/activities/{activity_id}")}
    if data:
        payload.update(data)
    return client.post(
        f"/activities/{activity_id}/enroll",
        data=payload,
        follow_redirects=follow_redirects,
    )


def enrollments(app, activity_id, status=None):
    with app.app_context():
        query = _db.select(Enrollment).where(Enrollment.activity_id == activity_id)
        if status is not None:
            query = query.where(Enrollment.status == status)
        return list(_db.session.execute(query).scalars().all())


def system_notices(app, user_id=None, subject_like=None):
    """系统通知（``sender_id IS NULL`` 的 Message）。"""
    with app.app_context():
        query = _db.select(Message).where(Message.sender_id.is_(None))
        if subject_like:
            query = query.where(Message.subject.like(f"%{subject_like}%"))
        if user_id is not None:
            query = query.join(
                MessageRecipient, MessageRecipient.message_id == Message.id
            ).where(MessageRecipient.user_id == user_id)
        return list(_db.session.execute(query).scalars().all())


def activity_form(category_id, **overrides):
    def local(hours):
        return to_local(utcnow() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")

    data = {
        "title": "V3.0 结构化地点测试",
        "category_id": str(category_id),
        "cover_url": "",
        "description": "",
        "start_time": local(96),
        "end_time": local(99),
        "enroll_deadline": local(72),
        "location": "",
        "campus": "东校区",
        "building": "图书馆",
        "room": "302",
        "location_note": "从南门进",
        "capacity": "20",
        "qualification": "",
        "action": "publish",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# DR-1 满员统一候补入口
# ---------------------------------------------------------------------------


def test_dr1_full_activity_always_enters_waitlist(
    client, login, csrf_token, student, teacher, activity_factory, app
):
    """满员时的报名必须是「候补已加入 + 第 N 位」，不是看不懂的失败。"""
    activity = activity_factory(
        teacher.id, capacity=1, enrolled_count=1, deadline_in_hours=48
    )
    login(student.student_no)

    resp = enroll_post(client, csrf_token, activity.id)
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "名额已满" in html
    assert "已进入候补队列" in html
    assert "第 1 位" in html
    assert [row.status for row in enrollments(app, activity.id)] == [
        EnrollStatus.WAITLISTED
    ]


def test_dr1_concurrent_full_enroll_has_zero_bare_errors(concurrent_app):
    """并发压测：40 人同时抢一个已满的活动，全部得到「已进入候补」。

    DR-1 的验收口径是「100% 得到明确结果，0 次裸错误」。真实数据里
    41 次 capacity_conflict 就是并发时序下"满员判定"与"候补入口"没接上。
    """
    thread_count = 40

    with concurrent_app.app_context():
        category = Category(name="并发满员", sort_order=0)
        organizer = User(student_no="org-v3", name="组织者", role=Role.TEACHER)
        organizer.set_password("Test-Password-2026")
        _db.session.add_all([category, organizer])
        _db.session.flush()

        now = utcnow()
        activity = Activity(
            title="满员并发压测",
            category_id=category.id,
            organizer_id=organizer.id,
            start_time=now + timedelta(hours=48),
            end_time=now + timedelta(hours=50),
            enroll_deadline=now + timedelta(hours=24),
            location="线上",
            capacity=1,
            enrolled_count=1,  # 一开始就满员
            status=ActivityStatus.PUBLISHED,
        )
        _db.session.add(activity)
        _db.session.flush()

        user_ids = []
        for index in range(thread_count):
            person = User(
                student_no=f"v3-full-{index:03d}",
                name=f"学生{index}",
                role=Role.STUDENT,
            )
            person.set_password("Test-Password-2026")
            _db.session.add(person)
            _db.session.flush()
            user_ids.append(person.id)
        _db.session.commit()
        activity_id = activity.id

    barrier = threading.Barrier(thread_count)
    results = []
    lock = threading.Lock()

    def worker(user_id):
        with concurrent_app.app_context():
            barrier.wait(timeout=30)
            ok, message = services.enroll(activity_id, user_id)
            with lock:
                results.append((ok, message))

    threads = [
        threading.Thread(target=worker, args=(user_id,)) for user_id in user_ids
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)

    assert len(results) == thread_count, "有线程没跑完"
    assert all(ok for ok, _ in results), "满员报名不该出现失败"
    assert all("候补" in message for _, message in results), "提示语必须说明已进入候补"

    with concurrent_app.app_context():
        activity = _db.session.get(Activity, activity_id)
        assert activity.enrolled_count == 1, "候补不能占正式名额（BR-1）"
        waitlisted = _db.session.execute(
            _db.select(_db.func.count())
            .select_from(Enrollment)
            .where(
                Enrollment.activity_id == activity_id,
                Enrollment.status == EnrollStatus.WAITLISTED,
            )
        ).scalar_one()
        assert waitlisted == thread_count


# ---------------------------------------------------------------------------
# DR-2 提交回执与防重复
# ---------------------------------------------------------------------------


def test_dr2_repeat_submit_reads_as_success_not_error(
    client, login, csrf_token, student, teacher, activity_factory, app
):
    """重复提交不产生第二条记录，提示语是「你已报名成功」而不是报错。"""
    activity = activity_factory(teacher.id, capacity=10, deadline_in_hours=48)
    login(student.student_no)

    enroll_post(client, csrf_token, activity.id)
    resp = enroll_post(client, csrf_token, activity.id)
    html = resp.get_data(as_text=True)

    assert "你已报名成功" in html
    assert "无需重复提交" in html
    assert "正式名额" in html
    assert len(enrollments(app, activity.id)) == 1


def test_dr2_repeat_submit_on_waitlist_shows_position(
    client, login, csrf_token, student, teacher, activity_factory
):
    activity = activity_factory(
        teacher.id, capacity=1, enrolled_count=1, deadline_in_hours=48
    )
    login(student.student_no)

    enroll_post(client, csrf_token, activity.id)
    resp = enroll_post(client, csrf_token, activity.id)
    html = resp.get_data(as_text=True)

    assert "你已在候补队列中" in html
    assert "第 1 位" in html


# ---------------------------------------------------------------------------
# DR-3 状态变更主动通知
# ---------------------------------------------------------------------------


def test_dr3_promotion_sends_a_notice_to_the_waitlisted_student(
    app, user_factory, teacher, activity_factory
):
    """候补转正必须主动通知：消息含活动名，且能在收件箱里查到。"""
    ahead = user_factory("20239001", "甲同学")
    waiting = user_factory("20239002", "乙同学")
    activity = activity_factory(teacher.id, capacity=1, deadline_in_hours=48)

    with app.app_context():
        assert services.enroll(activity.id, ahead.id)[0]
        ok, message = services.enroll(activity.id, waiting.id)
        assert ok and "候补" in message

        assert services.cancel_enrollment(activity.id, ahead.id)[0]
        # 乙应当已自动转正
        row = _db.session.execute(
            _db.select(Enrollment).where(
                Enrollment.activity_id == activity.id,
                Enrollment.user_id == waiting.id,
            )
        ).scalar_one()
        assert row.status == EnrollStatus.CONFIRMED
        assert row.promoted_at is not None, "候补转正时刻要落库（DR-4 的统计依据）"

        notices = system_notices(app, user_id=waiting.id, subject_like="候补转正")
        assert notices, "候补转正后必须产生站内通知"


def test_dr3_review_results_send_notices(app, user_factory, teacher, activity_factory):
    """审核通过 / 拒绝都要主动通知，不能只改状态。"""
    approved = user_factory("20239011", "通过同学")
    rejected = user_factory("20239012", "拒绝同学")
    activity = activity_factory(
        teacher.id, capacity=10, requires_review=True, deadline_in_hours=48
    )

    with app.app_context():
        assert services.enroll(activity.id, approved.id)[0]
        assert services.enroll(activity.id, rejected.id)[0]
        assert services.review_enrollment(activity.id, approved.id, teacher.id, True)[0]
        assert services.review_enrollment(activity.id, rejected.id, teacher.id, False)[0]

        assert system_notices(app, user_id=approved.id, subject_like="审核通过")
        assert system_notices(app, user_id=rejected.id, subject_like="审核未通过")


# ---------------------------------------------------------------------------
# DR-4 候补透明度
# ---------------------------------------------------------------------------


def test_dr4_waitlist_opportunity_rate_and_label(
    app, user_factory, teacher, activity_factory
):
    """转正率 = 转正数 / 候补历史总数；样本足够时给出机会标签。"""
    activity = activity_factory(teacher.id, capacity=10, deadline_in_hours=48)
    people = [
        user_factory(f"2023910{index}", f"候补{index}") for index in range(4)
    ]

    with app.app_context():
        base = utcnow() - timedelta(hours=5)
        for index, person in enumerate(people):
            # 前 2 人：候补转正；后 2 人：仍在候补
            promoted = index < 2
            _db.session.add(
                Enrollment(
                    activity_id=activity.id,
                    user_id=person.id,
                    status=(
                        EnrollStatus.CONFIRMED if promoted else EnrollStatus.WAITLISTED
                    ),
                    enrolled_at=base,
                    waitlisted_at=base + timedelta(minutes=index),
                    promoted_at=base + timedelta(minutes=30 + index)
                    if promoted
                    else None,
                )
            )
        _db.session.commit()

        info = waitlist_opportunity(activity.id)
        assert info["total"] == 4
        assert info["promoted"] == 2
        assert info["rate"] == 0.5
        assert info["label"] == "机会较大"


def test_dr4_small_sample_says_no_reference(
    app, user_factory, teacher, activity_factory
):
    """样本不足 3 人时不给比例——"1/1 = 100%"只会误导。"""
    student = user_factory("2023911x", "唯一候补")
    activity = activity_factory(teacher.id, capacity=10, deadline_in_hours=48)

    with app.app_context():
        _db.session.add(
            Enrollment(
                activity_id=activity.id,
                user_id=student.id,
                status=EnrollStatus.WAITLISTED,
                waitlisted_at=utcnow(),
            )
        )
        _db.session.commit()

        info = waitlist_opportunity(activity.id)
        assert info["total"] == 1
        assert info["rate"] is None
        assert info["label"] == "暂无参考"


# ---------------------------------------------------------------------------
# DR-5 指标采集
# ---------------------------------------------------------------------------


def test_dr5_enroll_records_runtime_metrics(
    client, login, csrf_token, student, teacher, activity_factory
):
    """每次报名都会进指标缓冲，管理端看板才有东西可看。"""
    metrics.reset()
    activity = activity_factory(teacher.id, capacity=10, deadline_in_hours=48)
    login(student.student_no)

    enroll_post(client, csrf_token, activity.id)

    snapshot = metrics.snapshot()
    assert snapshot["total"] >= 1
    assert snapshot["p95"] is not None
    assert snapshot["p95"] >= 0


# ---------------------------------------------------------------------------
# DR-6 递补实时化
# ---------------------------------------------------------------------------


def test_dr6_promotion_is_immediate_not_delayed(
    app, user_factory, teacher, activity_factory
):
    """取消成功后立刻查，候补已经是正式——不存在 120 秒的延迟。"""
    ahead = user_factory("20239201", "甲同学")
    waiting = user_factory("20239202", "乙同学")
    activity = activity_factory(teacher.id, capacity=1, deadline_in_hours=48)

    with app.app_context():
        services.enroll(activity.id, ahead.id)
        services.enroll(activity.id, waiting.id)
        services.cancel_enrollment(activity.id, ahead.id)

        # 同一个上下文里立刻查，不需要等待任何定时任务
        row = _db.session.execute(
            _db.select(Enrollment).where(
                Enrollment.activity_id == activity.id,
                Enrollment.user_id == waiting.id,
            )
        ).scalar_one()
        assert row.status == EnrollStatus.CONFIRMED
        assert row.promoted_at is not None


# ---------------------------------------------------------------------------
# DR-7 搜索与筛选
# ---------------------------------------------------------------------------


def test_dr7_weekend_and_enroll_state_filters(
    client, login, student, teacher, activity_factory
):
    """周末筛选与「可报名 / 已满可候补」筛选都要生效。"""
    local_now = to_local(utcnow())
    days_to_saturday = (5 - local_now.weekday()) % 7 or 7
    saturday = (local_now + timedelta(days=days_to_saturday)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    saturday_utc = parse_local(saturday.strftime("%Y-%m-%d %H:%M"))
    weekday_utc = saturday_utc - timedelta(days=2)  # 周四

    weekend_open = activity_factory(
        teacher.id,
        title="周末可报名",
        start_time=saturday_utc,
        end_time=saturday_utc + timedelta(hours=2),
        enroll_deadline=saturday_utc - timedelta(days=1),
        capacity=10,
    )
    weekend_full = activity_factory(
        teacher.id,
        title="周末已满员",
        start_time=saturday_utc + timedelta(hours=4),
        end_time=saturday_utc + timedelta(hours=6),
        enroll_deadline=saturday_utc - timedelta(days=1),
        capacity=2,
        enrolled_count=2,
    )
    activity_factory(
        teacher.id,
        title="工作日可报名",
        start_time=weekday_utc,
        end_time=weekday_utc + timedelta(hours=2),
        enroll_deadline=weekday_utc - timedelta(days=1),
        capacity=10,
    )

    login(student.student_no)

    weekend_page = client.get("/activities?weekend=1").get_data(as_text=True)
    assert "周末可报名" in weekend_page
    assert "周末已满员" in weekend_page
    assert "工作日可报名" not in weekend_page

    waitlist_page = client.get("/activities?enroll_state=waitlist").get_data(
        as_text=True
    )
    assert "周末已满员" in waitlist_page
    assert "周末可报名" not in waitlist_page
    assert "工作日可报名" not in waitlist_page

    open_page = client.get("/activities?enroll_state=open").get_data(as_text=True)
    assert "周末可报名" in open_page
    assert "周末已满员" not in open_page

    date_page = client.get(
        f"/activities?date={saturday.strftime('%Y-%m-%d')}"
    ).get_data(as_text=True)
    assert "周末可报名" in date_page
    assert "工作日可报名" not in date_page


# ---------------------------------------------------------------------------
# DR-8 服务端错误兜底
# ---------------------------------------------------------------------------


def test_dr8_internal_error_page_gives_a_readable_reason_and_retry(app, client):
    """500 时给可读页面 + 重试按钮，不要白屏。"""
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.route("/__v3_boom__")
    def boom():  # pragma: no cover - 只在请求时触发
        raise RuntimeError("boom")

    resp = client.get("/__v3_boom__")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 500
    assert "服务器开小差了" in body
    assert "重试" in body


# ---------------------------------------------------------------------------
# DR-10 结构化地点
# ---------------------------------------------------------------------------


def test_dr10_structured_location_is_composed(
    client, login, csrf_token, teacher, category, app
):
    """校区 / 建筑 / 房间 / 补充说明会被拼成活动地点。"""
    data = activity_form(category.id)
    login(teacher.student_no)

    resp = client.post(
        "/activities",
        data={**data, "csrf_token": csrf_token("/dashboard")},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        activity = _db.session.execute(
            _db.select(Activity).where(Activity.title == data["title"])
        ).scalar_one()
        assert activity.location == "东校区图书馆302（从南门进）"


def test_dr10_plain_location_still_works(
    client, login, csrf_token, teacher, category, app
):
    """不填结构化字段时退回手填地点——老活动编辑不受影响。"""
    data = activity_form(
        category.id,
        title="手填地点也能用",
        campus="",
        building="",
        room="",
        location_note="",
        location="老地方 101",
    )
    login(teacher.student_no)
    client.post(
        "/activities",
        data={**data, "csrf_token": csrf_token("/dashboard")},
        follow_redirects=True,
    )

    with app.app_context():
        activity = _db.session.execute(
            _db.select(Activity).where(Activity.title == "手填地点也能用")
        ).scalar_one()
        assert activity.location == "老地方 101"


# ---------------------------------------------------------------------------
# DR-11 时间冲突二次确认
# ---------------------------------------------------------------------------


def test_dr11_same_day_conflict_asks_for_confirmation_first(
    client, login, csrf_token, student, teacher, activity_factory, app
):
    # 用「本地时间的同一天」构造——直接按 UTC 加小时会跨过本地午夜，
    # 变成「不是同一天」，测出来的就不是要测的东西了。
    day = (to_local(utcnow()) + timedelta(days=2)).strftime("%Y-%m-%d")
    first_start = parse_local(f"{day} 09:00")
    second_start = parse_local(f"{day} 14:00")
    deadline = first_start - timedelta(days=1)

    first = activity_factory(
        teacher.id,
        title="上午的讲座",
        start_time=first_start,
        end_time=first_start + timedelta(hours=2),
        enroll_deadline=deadline,
        capacity=10,
    )
    second = activity_factory(
        teacher.id,
        title="下午的工作坊",
        start_time=second_start,
        end_time=second_start + timedelta(hours=2),
        enroll_deadline=deadline,
        capacity=10,
    )
    login(student.student_no)

    assert enroll_post(client, csrf_token, first.id).status_code == 200
    assert [row.status for row in enrollments(app, first.id)] == [
        EnrollStatus.CONFIRMED
    ]

    # 第二次报名：先返回确认页，还没有落库
    resp = enroll_post(client, csrf_token, second.id)
    html = resp.get_data(as_text=True)
    assert "确认报名" in html
    assert "上午的讲座" in html
    assert "时间冲突" in html or "同一天" in html
    assert enrollments(app, second.id) == []

    # 确认后才真正报名
    enroll_post(client, csrf_token, second.id, {"confirm": "1"})
    assert [row.status for row in enrollments(app, second.id)] == [
        EnrollStatus.CONFIRMED
    ]


# ---------------------------------------------------------------------------
# DR-12 热度与容量参考
# ---------------------------------------------------------------------------


def test_dr12_heat_labels(app, teacher, activity_factory):
    general = activity_factory(teacher.id, capacity=10, enrolled_count=1)
    hot = activity_factory(teacher.id, capacity=10, enrolled_count=9)
    full = activity_factory(teacher.id, capacity=2, enrolled_count=2)

    with app.app_context():
        assert activity_heat(_db.session.get(Activity, general.id))["label"] == "一般"
        assert activity_heat(_db.session.get(Activity, hot.id))["label"] == "即将满员"
        assert activity_heat(_db.session.get(Activity, full.id))["label"] == "已满员"


def test_dr12_capacity_advice_uses_same_category_demand(
    app, teacher, category, activity_factory
):
    activity_factory(
        teacher.id, capacity=10, enrolled_count=10, category_id=category.id
    )
    activity_factory(
        teacher.id, capacity=20, enrolled_count=20, category_id=category.id
    )

    with app.app_context():
        advice = capacity_advice(category.id)
        assert advice is not None
        assert advice["sample"] == 2
        assert advice["average"] == 15
        assert advice["low"] <= advice["high"]
