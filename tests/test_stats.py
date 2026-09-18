"""M5：统计口径与 CSV 导出。

统计最容易出的不是「算不出来」，而是**算得太顺**——口径没对齐，数字看着
都合理，用起来全是错的。所以这里逐条钉住口径：

* 报名人数 = 状态为「已报名」的记录数（「已取消」不计入）；
* 「缺席」计入报名总数，但不计入实到人数；
* 汇总的率是**加总后再算**，不是各活动率的平均。
"""

import pytest

from app.models import EnrollStatus
from app.services import activity_stats, organizer_overview


def post(client, csrf_token, path, follow_redirects=False, **data):
    payload = {"csrf_token": csrf_token("/dashboard")}
    payload.update(data)
    return client.post(path, data=payload, follow_redirects=follow_redirects)


# ---------------------------------------------------------------------------
# 指标口径
# ---------------------------------------------------------------------------


def test_stats_counts_absent_in_enrolled_but_not_in_attended(app, club, student, user_factory, activity_factory, enroll_factory):
    """报名 4 人、实到 3 人 → 签到率 75%，缺席 1 人仍计入报名。"""
    activity = activity_factory(club.id, capacity=10)
    others = [user_factory(f"2023{i:04d}", f"学生{i}", "STUDENT") for i in range(3)]
    everyone = [student.id] + [u.id for u in others]

    # 前 3 个人签到，最后 1 个缺席
    enroll_factory(activity.id, everyone, checked_in=everyone[:3])

    with app.app_context():
        from app.extensions import db as _db
        from app.models import Activity

        stats = activity_stats(_db.session.get(Activity, activity.id))

    assert stats["enrolled"] == 4
    assert stats["checked_in"] == 3
    assert stats["absent"] == 1
    assert stats["check_in_rate"] == 75.0
    assert stats["fill_rate"] == 40.0


def test_cancelled_enrollments_are_excluded(app, club, student, activity_factory, enroll_factory):
    """「已取消」的记录不计入报名总数。"""
    activity = activity_factory(club.id, capacity=10)
    enroll_factory(activity.id, [student.id], status=EnrollStatus.CANCELED)

    with app.app_context():
        from app.extensions import db as _db
        from app.models import Activity

        stats = activity_stats(_db.session.get(Activity, activity.id))

    assert stats["enrolled"] == 0
    assert stats["check_in_rate"] == 0.0  # 分母为 0 时不能崩


def test_rates_do_not_divide_by_zero(app, club, activity_factory):
    activity = activity_factory(club.id, capacity=10)
    with app.app_context():
        from app.extensions import db as _db
        from app.models import Activity

        stats = activity_stats(_db.session.get(Activity, activity.id))

    assert stats["check_in_rate"] == 0.0
    assert stats["fill_rate"] == 0.0


def test_organizer_overview_aggregates_before_dividing(app, club, student, user_factory, activity_factory, enroll_factory):
    """汇总必须是「加总后再算率」，不能是各活动率的平均。

    构造一个能区分两者的场景：大活动 4 人报名 1 人到场（25%），
    小活动 1 人报名 1 人到场（100%）。

    * 加总口径：2 / 5 = **40%** —— 这才是真实的到场情况；
    * 取平均：(25 + 100) / 2 = 62.5% —— 一场 1 人的小活动，把整体拉高了 22 个点。
    """
    big = activity_factory(club.id, capacity=100)
    small = activity_factory(club.id, capacity=10)

    bunch = [user_factory(f"b{i:04d}", f"大活动学生{i}", "STUDENT") for i in range(4)]
    enroll_factory(big.id, [u.id for u in bunch], checked_in=[bunch[0].id])
    enroll_factory(small.id, [student.id], checked_in=[student.id])

    with app.app_context():
        overview = organizer_overview([big.id, small.id])

    assert overview["enrolled"] == 5
    assert overview["checked_in"] == 2
    assert overview["check_in_rate"] == 40.0


def test_stats_page_shows_both_indicators(client, login, club, student, activity_factory, enroll_factory):
    activity = activity_factory(club.id, capacity=10)
    enroll_factory(activity.id, [student.id], checked_in=[student.id])

    login(club.student_no)
    html = client.get(f"/activities/{activity.id}/stats").get_data(as_text=True)

    assert "签到率" in html
    assert "上座率" in html
    assert "100.0%" in html


def test_organizer_stats_page_lists_every_activity(client, login, club, activity_factory):
    activity_factory(club.id, title="统计页活动甲")
    activity_factory(club.id, title="统计页活动乙")

    login(club.student_no)
    html = client.get("/club/stats").get_data(as_text=True)

    assert "统计页活动甲" in html and "统计页活动乙" in html
    assert "整体签到率" in html


def test_stats_are_limited_to_the_owner(
    client, login, student, user_factory, activity_factory, enroll_factory
):
    """查看统计是「创建者或管理员」的权力。"""
    stranger = user_factory("club011", "天文社", "CLUB")
    activity = activity_factory(stranger.id)

    login(student.student_no)
    assert client.get(f"/activities/{activity.id}/stats").status_code == 403


# ---------------------------------------------------------------------------
# CSV 导出
# ---------------------------------------------------------------------------


def test_csv_export_has_bom_and_correct_rows(
    client, login, club, student, user_factory, activity_factory, enroll_factory
):
    """导出名单必须是带 BOM 的 UTF-8，行数等于报名人数 + 表头。

    没有 BOM 的话，Excel 会按本地编码（简体中文 Windows 上是 GBK）去读，
    中文全部乱码——这是「用 Excel 打开导出文件」最常见的翻车方式。
    """
    activity = activity_factory(club.id, title="导出测试活动", capacity=10)
    others = [user_factory(f"2025{i:04d}", f"名单学生{i}", "STUDENT") for i in range(2)]
    everyone = [student.id] + [u.id for u in others]
    enroll_factory(activity.id, everyone, checked_in=[student.id])

    login(club.student_no)
    resp = client.get(f"/activities/{activity.id}/enrollments/export")

    assert resp.status_code == 200
    assert "text/csv" in resp.headers["Content-Type"]
    assert "attachment" in resp.headers["Content-Disposition"]

    raw = resp.get_data()
    assert raw.startswith(b"\xef\xbb\xbf"), "CSV 缺少 UTF-8 BOM，Excel 打开会乱码"

    text = raw.decode("utf-8-sig")
    lines = text.split("\r\n")
    assert lines[0].startswith("序号,学号,姓名,报名时间,签到状态,签到时间")
    assert len(lines) == 3 + 1, "行数应为「表头 + 报名人数」"
    assert "已签到" in text and "未签到" in text


def test_csv_escapes_fields_containing_commas(
    client, login, club, user_factory, activity_factory, enroll_factory
):
    """姓名里带逗号不能把整张表挤歪一列。"""
    activity = activity_factory(club.id, capacity=10)
    odd = user_factory("20260001", "张三,李四", "STUDENT")
    enroll_factory(activity.id, [odd.id])

    login(club.student_no)
    text = (
        client.get(f"/activities/{activity.id}/enrollments/export")
        .get_data()
        .decode("utf-8-sig")
    )
    assert '"张三,李四"' in text


def test_csv_export_requires_ownership(
    client, login, student, user_factory, activity_factory
):
    stranger = user_factory("club012", "棋类社", "CLUB")
    activity = activity_factory(stranger.id)

    login(student.student_no)
    assert client.get(f"/activities/{activity.id}/enrollments/export").status_code == 403


def test_exported_rows_match_the_roster(
    client, login, club, student, user_factory, activity_factory, enroll_factory
):
    """名单上「已取消」的记录不能出现在导出里。"""
    activity = activity_factory(club.id, capacity=10)
    keep = user_factory("20270001", "保留的人", "STUDENT")
    dropped = user_factory("20270002", "取消的人", "STUDENT")
    enroll_factory(activity.id, [keep.id])
    enroll_factory(activity.id, [dropped.id], status=EnrollStatus.CANCELED)

    login(club.student_no)
    text = (
        client.get(f"/activities/{activity.id}/enrollments/export")
        .get_data()
        .decode("utf-8-sig")
    )

    assert "保留的人" in text
    assert "取消的人" not in text
