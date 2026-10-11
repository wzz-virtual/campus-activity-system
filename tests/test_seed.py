"""M6：演示数据播种的联调冒烟（V2.0）。

`flask seed-demo` 是开发者拿到项目后第一件要跑的命令。它一旦坏了，
后面所有人都卡在「没有账号、没法试」。所以这里把它当成一条真实链路来测：
播种 → 三种角色登录 → 走一遍主闭环。

V2.0 的演示数据要点名覆盖三类平时很难自然出现的场景：

* **满员 + 候补队列**（暗房冲洗体验）——演示「候补中 · 第 N 位」；
* **满员 + 需审核**（胶片冲洗进阶班）——演示「通过 ≠ 一定参加」；
* **需审核 + 待审核申请**（人像摄影体验日、天文观测夜）——演示逐人审核；
* **被管理员下线的活动**（电竞友谊赛）——演示「处理标记」与学生端不可见；
* **五种报名状态同屏**（20230114 的「我的报名」）——一页看全 V2.0 的五态。
"""

import re
from datetime import timedelta

from app.extensions import db as _db
from app.models import (
    Activity,
    ActivityStatus,
    AuditAction,
    AuditLog,
    Category,
    EnrollStatus,
    Enrollment,
    Role,
    User,
    utcnow,
)
from app.seed import ACTIVITY_COUNT, DEMO_PASSWORD


def csrf_of(client, path):
    html = client.get(path).get_data(as_text=True)
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    assert match, f"{path} 页面里没有 csrf_token"
    return match.group(1)


def count(model):
    return _db.session.execute(_db.select(_db.func.count()).select_from(model)).scalar_one()


def test_seed_demo_command_builds_a_usable_dataset(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["seed-demo"])

    assert result.exit_code == 0, result.output
    assert DEMO_PASSWORD in result.output

    with app.app_context():
        assert count(Category) == 4
        assert count(Activity) == ACTIVITY_COUNT

        statuses = {
            status
            for (status,)
            in _db.session.execute(_db.select(Activity.status).distinct())
        }
        # 六种活动状态全部覆盖，否则界面上有一半分支没数据可看
        assert statuses == set(ActivityStatus.ALL)

        # 三种角色都在
        roles = {
            role for (role,) in _db.session.execute(_db.select(User.role).distinct())
        }
        assert roles == set(Role.ALL)

        # 冗余计数与真实「正式」报名数必须对得上——这是并发控制的载体。
        # 注意：候补与待审核**不占名额**，不能算进去。
        for activity in _db.session.execute(_db.select(Activity)).scalars():
            confirmed = _db.session.execute(
                _db.select(_db.func.count())
                .select_from(Enrollment)
                .where(
                    Enrollment.activity_id == activity.id,
                    Enrollment.status == EnrollStatus.CONFIRMED,
                )
            ).scalar_one()
            assert activity.enrolled_count == confirmed, (
                f"活动 {activity.title} 的 enrolled_count 与真实正式报名数不一致"
            )


def test_seeded_dataset_demos_waitlist_and_review_paths(app):
    """V2.0 新增能力的演示数据必须存在，否则打开页面看不到这些功能。"""
    app.test_cli_runner().invoke(args=["seed-demo"])

    with app.app_context():
        workshop = _db.session.execute(
            _db.select(Activity).where(Activity.title == "暗房冲洗体验（小班）")
        ).scalar_one()
        waitlisted = _db.session.execute(
            _db.select(Enrollment).where(
                Enrollment.activity_id == workshop.id,
                Enrollment.status == EnrollStatus.WAITLISTED,
            )
        ).scalars().all()
        assert len(waitlisted) >= 3, "满员活动应当预置候补队列，用于演示排位"

        portrait = _db.session.execute(
            _db.select(Activity).where(Activity.title == "人像摄影体验日")
        ).scalar_one()
        assert portrait.requires_review is True
        assert portrait.qualification, "需审核的活动必须带参加资格说明"
        pending = _db.session.execute(
            _db.select(Enrollment).where(
                Enrollment.activity_id == portrait.id,
                Enrollment.status == EnrollStatus.PENDING,
            )
        ).scalars().all()
        assert pending, "需审核的活动应当预置待审核申请，用于演示审核流程"


def test_seeded_full_review_activity_demos_approve_then_waitlist(app):
    """「胶片冲洗进阶班」：满员 + 需审核（TC-8 的演示场景）。

    上限只有 3 人且已经满员——教师"通过"的那一刻没有余位，学生只能排进候补。
    没有这个场景，"通过 ≠ 一定参加"就只能靠口头讲。
    """
    app.test_cli_runner().invoke(args=["seed-demo"])

    with app.app_context():
        advanced = _db.session.execute(
            _db.select(Activity).where(
                Activity.title == "胶片冲洗进阶班（限 3 人 · 需审核）"
            )
        ).scalar_one()

        assert advanced.requires_review is True
        assert advanced.qualification
        assert advanced.enrolled_count == advanced.capacity == 3, "这个演示场景必须是满员的"

        pending = _db.session.execute(
            _db.select(Enrollment).where(
                Enrollment.activity_id == advanced.id,
                Enrollment.status == EnrollStatus.PENDING,
            )
        ).scalars().all()
        assert pending, "满员 + 待审核申请，才能演示「通过后进候补」"


def test_seeded_demo_account_shows_all_five_enrollment_states(app):
    """20230114 的「我的报名」要一页看全五种状态——V2.0 的重点展示对象。

    正式 / 候补 / 待审核 / 已拒绝 / 已取消，五种状态同屏，不用来回切换账号。
    """
    app.test_cli_runner().invoke(args=["seed-demo"])

    with app.app_context():
        fan = _db.session.execute(
            _db.select(User).where(User.student_no == "20230114")
        ).scalar_one()
        statuses = {
            row.status
            for row in _db.session.execute(
                _db.select(Enrollment).where(Enrollment.user_id == fan.id)
            ).scalars()
        }

    assert statuses == set(EnrollStatus.ALL), (
        f"演示账号应当覆盖五种报名状态，实际只有：{sorted(statuses)}"
    )


def test_seeded_dataset_covers_plaza_and_interception_paths(app, client, login):
    app.test_cli_runner().invoke(args=["seed-demo"])

    # 刻意挑一个**没有**报名这两场活动的学生：已经报名的人看到的是
    # 「你已报名」，看不到「已满员 / 报名已截止」这两个拦截分支
    login("20230110", password=DEMO_PASSWORD)

    plaza = client.get("/activities").get_data(as_text=True)
    assert "手机摄影技巧分享会" in plaza
    assert "暗房冲洗体验（小班）" in plaza
    # 草稿、已下线不该出现在广场上
    assert "校园夜景采风" not in plaza
    assert "电竞友谊赛" not in plaza
    # 需审核的活动是"已发布"，应当可见，并带「需审核」标记
    assert "人像摄影体验日" in plaza
    assert "需审核" in plaza

    with app.app_context():
        full = _db.session.execute(
            _db.select(Activity).where(Activity.title == "暗房冲洗体验（小班）")
        ).scalar_one()
        closed = _db.session.execute(
            _db.select(Activity).where(Activity.title == "校运会摄影志愿服务")
        ).scalar_one()
        full_id, closed_id = full.id, closed.id

    # 「已满员 · 可候补」和「报名已截止」这两个分支平时很难自然出现，
    # 播种数据就是为了让人能亲眼确认提示语是对的
    full_page = client.get(f"/activities/{full_id}").get_data(as_text=True)
    assert "已满员" in full_page
    assert "加入候补队列" in full_page, "满员时应当给出候补入口（BR-3）"

    closed_page = client.get(f"/activities/{closed_id}").get_data(as_text=True)
    assert "报名已截止" in closed_page


def test_seeded_ongoing_activity_has_a_check_in_code(app, client, login):
    app.test_cli_runner().invoke(args=["seed-demo"])

    with app.app_context():
        ongoing = _db.session.execute(
            _db.select(Activity).where(Activity.status == ActivityStatus.ONGOING)
        ).scalar_one()
        activity_id, code = ongoing.id, ongoing.check_in_code

    assert code and len(code) == 6

    login("20230101", password=DEMO_PASSWORD)
    page = client.get(f"/activities/{activity_id}/check-in").get_data(as_text=True)
    assert "签到" in page


def test_seeded_ongoing_activity_stays_ongoing_for_hours(app):
    """「进行中」的演示活动不能一个小时后就结束。

    这是演示数据里**唯一**一场进行中的活动，「凭签到码签到」整条路径都靠它。
    最初把它设成「播种后 1 小时结束」，结果过了 1 小时再看：活动已经变成
    「已结束」，签到通道关闭，页面上只会写「活动已结束」——
    看到的人很容易以为这是功能坏了，而不是数据过期了。

    所以这里钉住「播种后至少还能撑两小时」，避免这类回归悄悄发生。
    """
    app.test_cli_runner().invoke(args=["seed-demo"])

    with app.app_context():
        ongoing = _db.session.execute(
            _db.select(Activity).where(Activity.status == ActivityStatus.ONGOING)
        ).scalar_one()
        remaining = ongoing.end_time - utcnow()

    assert remaining >= timedelta(hours=2), (
        f"进行中的演示活动只剩 {remaining} 就结束了，"
        "签到演示路径会在演示到一半时失效"
    )


def test_seeded_accounts_can_all_log_in(app, client, login):
    """三个角色都能登录，并且各自的落地页能打开。"""
    app.test_cli_runner().invoke(args=["seed-demo"])

    for student_no, landing in [
        ("20230101", "/activities"),
        ("club001", "/my/activities"),
        ("admin", "/admin/activities"),
    ]:
        resp = login(student_no, password=DEMO_PASSWORD)
        assert resp.status_code == 302, f"{student_no} 登录失败"

        assert client.get(landing).status_code == 200, f"{student_no} 打不开 {landing}"


def test_seed_refuses_to_run_twice(app):
    """已有数据时直接报错，而不是把真实数据盖掉。"""
    runner = app.test_cli_runner()
    assert runner.invoke(args=["seed-demo"]).exit_code == 0

    second = runner.invoke(args=["seed-demo"])
    assert second.exit_code != 0
    assert "已经有用户" in second.output
    assert "--reset" in second.output, "报错里要给出重播的办法"

    with app.app_context():
        assert count(Activity) == ACTIVITY_COUNT


def test_seed_reset_rebuilds_the_dataset(app):
    """``--reset`` 先清空再重播。

    演示数据的时间是按「相对播种那一刻」算的，放着不动会逐天过期；
    过期之后页面上只剩「已结束」「报名已截止」，看着像功能坏了。
    所以要有一个一键重播的入口，而不是让人手动删数据库文件。
    """
    runner = app.test_cli_runner()
    assert runner.invoke(args=["seed-demo"]).exit_code == 0

    with app.app_context():
        # 人为破坏：删掉全部活动，模拟「数据被改乱了」或「放太久过期了」
        _db.session.execute(_db.delete(Enrollment))
        _db.session.execute(_db.delete(Activity))
        _db.session.commit()
        assert count(Activity) == 0

    result = runner.invoke(args=["seed-demo", "--reset"])
    assert result.exit_code == 0, result.output
    assert "已清空全部数据表" in result.output

    with app.app_context():
        assert count(Activity) == ACTIVITY_COUNT
        assert count(Category) == 4
        # 重播之后，「进行中」的那场必须回来了——签到演示全靠它
        ongoing = _db.session.execute(
            _db.select(Activity).where(Activity.status == ActivityStatus.ONGOING)
        ).scalar_one()
        assert ongoing.check_in_code and len(ongoing.check_in_code) == 6
        assert ongoing.end_time - utcnow() >= timedelta(hours=2)


def test_seed_writes_a_consistent_audit_trail(app):
    """演示数据要带上活动流转日志，否则「处理标记」那一格是空的。

    种子是直接把活动建成「已下线 / 已取消」这些状态的，绕过了真实的
    发布 / 下线 / 取消操作。不补日志的话，被下线活动的「处理标记」就是
    空白——明明有一场已下线的活动，界面上却看不到是谁、为什么下的线。

    这个缺陷是在浏览器里看页面截图时发现的，接口层断言抓不到：
    页面 200、字段都在，只是那一格是空的。
    """
    app.test_cli_runner().invoke(args=["seed-demo"])

    with app.app_context():
        logs = _db.session.execute(_db.select(AuditLog)).scalars().all()
        assert logs, "流转日志一条都没有，处理标记会显示成空白"

        by_activity = {}
        for entry in logs:
            by_activity.setdefault(entry.activity_id, []).append(entry)

        # 已下线的活动：必须有 PUBLISH + OFFLINE（且 OFFLINE 带原因）
        offlined = _db.session.execute(
            _db.select(Activity).where(Activity.status == ActivityStatus.OFFLINE)
        ).scalar_one()
        actions = [e.action for e in by_activity[offlined.id]]
        assert AuditAction.PUBLISH in actions
        offline_logs = [e for e in by_activity[offlined.id] if e.action == AuditAction.OFFLINE]
        assert offline_logs and offline_logs[0].reason, "下线记录必须带原因（TC-14）"

        # 已取消的活动：必须有取消记录
        canceled = _db.session.execute(
            _db.select(Activity).where(Activity.status == ActivityStatus.CANCELED)
        ).scalar_one()
        assert AuditAction.CANCEL in [e.action for e in by_activity[canceled.id]]

        # 草稿：没有任何流转，不该有日志
        draft = _db.session.execute(
            _db.select(Activity).where(Activity.status == ActivityStatus.DRAFT)
        ).scalar_one()
        assert draft.id not in by_activity, "草稿没有任何流转，不该有日志"

        # 日志时间必须都在「现在」之前：出现未来时间会让人以为服务器时钟错了
        for entry in logs:
            assert entry.created_at <= utcnow() + timedelta(minutes=1), entry


def test_seeded_offline_marker_is_visible_on_pages(app, client, login):
    """页面上那一格不能再是空的——处理标记要真的渲染出来。"""
    app.test_cli_runner().invoke(args=["seed-demo"])

    login("admin", password=DEMO_PASSWORD)
    html = client.get("/admin/activities").get_data(as_text=True)
    assert "下线处理：" in html, "活动监督页应当显示下线原因与操作人"

    login("club001", password=DEMO_PASSWORD)
    mine = client.get("/my/activities").get_data(as_text=True)
    assert "已被管理员下线" in mine
