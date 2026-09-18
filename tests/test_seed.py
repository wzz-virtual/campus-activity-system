"""M6：演示数据播种的联调冒烟。

`flask seed-demo` 是开发者拿到项目后第一件要跑的命令。它一旦坏了，
后面所有人都卡在「没有账号、没法试」。所以这里把它当成一条真实链路来测：
播种 → 三种角色登录 → 走一遍主闭环。
"""

import re

from app.extensions import db as _db
from app.models import Activity, ActivityStatus, Category, Enrollment
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
            for (status,) in _db.session.execute(_db.select(Activity.status).distinct())
        }
        # 七种状态全部覆盖，否则界面上有一半分支没数据可看
        assert statuses == set(ActivityStatus.ALL)

        # 报名记录与冗余计数必须对得上——这是并发控制的载体
        for activity in _db.session.execute(_db.select(Activity)).scalars():
            actual = _db.session.execute(
                _db.select(_db.func.count())
                .select_from(Enrollment)
                .where(Enrollment.activity_id == activity.id)
            ).scalar_one()
            assert activity.enrolled_count == actual, (
                f"活动 {activity.title} 的 enrolled_count 与真实报名数不一致"
            )


def test_seeded_dataset_covers_plaza_and_interception_paths(app, client, login):
    app.test_cli_runner().invoke(args=["seed-demo"])

    # 刻意挑一个**没有**报名这两场活动的学生：已经报名的人看到的是
    # 「你已报名」，看不到「名额已满 / 报名已截止」这两个拦截分支
    login("20230110", password=DEMO_PASSWORD)

    plaza = client.get("/activities").get_data(as_text=True)
    assert "手机摄影技巧分享会" in plaza
    assert "暗房冲洗体验（小班）" in plaza
    # 草稿 / 待审核 / 已驳回不该出现在广场上
    assert "校园夜景采风" not in plaza
    assert "人像摄影体验日" not in plaza
    assert "电竞友谊赛" not in plaza

    with app.app_context():
        full = _db.session.execute(
            _db.select(Activity).where(Activity.title == "暗房冲洗体验（小班）")
        ).scalar_one()
        closed = _db.session.execute(
            _db.select(Activity).where(Activity.title == "校运会摄影志愿服务")
        ).scalar_one()
        full_id, closed_id = full.id, closed.id

    # 「名额已满」和「报名已截止」这两个分支平时很难自然出现，
    # 播种数据就是为了让人能亲眼确认提示语是对的
    full_page = client.get(f"/activities/{full_id}").get_data(as_text=True)
    assert "名额已满" in full_page

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


def test_seeded_accounts_can_all_log_in(app, client, login):
    """三个角色都能登录，并且各自的首页能打开。"""
    app.test_cli_runner().invoke(args=["seed-demo"])

    for student_no, landing in [
        ("20230101", "/activities"),
        ("club001", "/my/activities"),
        ("admin", "/admin/audit"),
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

    with app.app_context():
        assert count(Activity) == ACTIVITY_COUNT


def test_seeded_admin_audit_queue_is_noisy_enough_to_be_useful(app, client, login):
    app.test_cli_runner().invoke(args=["seed-demo"])

    login("admin", password=DEMO_PASSWORD)
    html = client.get("/admin/audit").get_data(as_text=True)

    assert "人像摄影体验日" in html
    assert "天文观测夜" in html
    assert "提交人" in html
