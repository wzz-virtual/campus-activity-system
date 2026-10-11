"""M1：活动创建与发布、活动列表 / 详情。

覆盖点：

* 管理员创建 → 直接发布；社团创建 → 待审核；社团可先存草稿；
* 表单校验的每一条规则，以及**校验失败不清空已填内容**；
* 列表的筛选、分页、以及「草稿 / 待审核 / 已驳回对学生完全不可见」；
* 编辑的权限（归谁、什么状态能改）与删除草稿；
* 时间入库转 UTC、按状态自动推进。
"""

import re
from datetime import timedelta

import pytest

from app.models import ActivityStatus, utcnow
from app.timeutils import to_local


def local_input(hours_from_now):
    """把「距今多少小时」写成 datetime-local 需要的本地时间字符串。"""
    return to_local(utcnow() + timedelta(hours=hours_from_now)).strftime(
        "%Y-%m-%dT%H:%M"
    )


def activity_form(category_id, **overrides):
    data = {
        "title": "手机摄影技巧分享会",
        "category_id": str(category_id),
        "cover_url": "",
        "description": "面向零基础同学，讲构图与用光。",
        "start_time": local_input(96),
        "end_time": local_input(99),
        "enroll_deadline": local_input(72),
        "location": "传媒楼 201",
        "capacity": "40",
        "action": "submit",
    }
    data.update(overrides)
    return data


def post_activity(client, csrf_token, data, path="/activities"):
    payload = dict(data)
    payload["csrf_token"] = csrf_token("/dashboard")
    return client.post(path, data=payload)


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------


def test_admin_created_activity_is_published_immediately(
    client, login, csrf_token, admin, category, activity_state
):
    """管理员创建的活动跳过审核，直接发布。"""
    login(admin.student_no)
    resp = post_activity(client, csrf_token, activity_form(category.id))

    assert resp.status_code == 302
    # 302 的 Location 里带着新活动的 id，从地址里把它取出来
    activity_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    assert activity_state(activity_id).status == ActivityStatus.PUBLISHED


def test_club_created_activity_waits_for_review(
    client, login, csrf_token, club, category, activity_state
):
    """社团创建的活动一律进「待审核」，此时不该出现在活动广场。"""
    login(club.student_no)
    resp = post_activity(client, csrf_token, activity_form(category.id))

    activity_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    assert activity_state(activity_id).status == ActivityStatus.PENDING

    plaza = client.get("/activities").get_data(as_text=True)
    assert "手机摄影技巧分享会" not in plaza


def test_club_can_save_draft_without_admin_attention(
    client, login, csrf_token, club, category, activity_state
):
    """存草稿：字段可以没填完，先进 DRAFT，不惊动管理员。"""
    login(club.student_no)
    data = activity_form(category.id, action="draft", title="", location="")
    resp = post_activity(client, csrf_token, data)

    activity_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    state = activity_state(activity_id)
    assert state.status == ActivityStatus.DRAFT
    # 数据库里这几个列是 NOT NULL，草稿要有兜底值
    assert state.title


def test_student_cannot_create_activity(client, login, csrf_token, student, category):
    """学生连表单页都进不去，更不用说提交。"""
    login(student.student_no)
    assert client.get("/activities/new").status_code == 403
    assert post_activity(client, csrf_token, activity_form(category.id)).status_code == 403


# ---------------------------------------------------------------------------
# 表单校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "override,expected_message",
    [
        ({"title": ""}, "活动标题不能为空"),
        ({"title": "标" * 101}, "不能超过 100 字"),
        ({"location": ""}, "活动地点不能为空"),
        ({"capacity": "0"}, "人数上限必须是大于 0 的整数"),
        ({"capacity": "-5"}, "人数上限必须是大于 0 的整数"),
        ({"capacity": "abc"}, "人数上限必须是大于 0 的整数"),
        ({"start_time": ""}, "请选择开始时间"),
        ({"start_time": "不是时间"}, "开始时间格式不正确"),
        # 结束早于开始
        ({"start_time": local_input(96), "end_time": local_input(90)}, "结束时间必须晚于开始时间"),
        # 报名截止晚于开始
        ({"start_time": local_input(96), "enroll_deadline": local_input(120)}, "报名截止时间不能晚于活动开始时间"),
    ],
)
def test_form_validation_rejects_bad_input(
    client, login, csrf_token, club, category, override, expected_message
):
    login(club.student_no)
    resp = post_activity(client, csrf_token, activity_form(category.id, **override))

    assert resp.status_code == 400
    assert expected_message in resp.get_data(as_text=True)


def test_validation_failure_keeps_what_the_user_typed(
    client, login, csrf_token, club, category
):
    """校验失败必须保留已填内容——让人从头再填一遍最容易招致放弃。"""
    login(club.student_no)
    resp = post_activity(
        client,
        csrf_token,
        activity_form(
            category.id,
            title="摄影作品点评会",
            location="传媒楼 201",
            capacity="40",
            start_time=local_input(96),
            end_time=local_input(90),  # 故意写反，触发校验失败
        ),
    )
    html = resp.get_data(as_text=True)

    assert resp.status_code == 400
    assert "结束时间必须晚于开始时间" in html
    assert 'value="摄影作品点评会"' in html
    assert 'value="传媒楼 201"' in html
    assert 'value="40"' in html


def test_unknown_category_is_rejected(client, login, csrf_token, club):
    login(club.student_no)
    resp = post_activity(client, csrf_token, activity_form(999999))
    assert resp.status_code == 400
    assert "所选分类不存在" in resp.get_data(as_text=True)


def test_start_time_is_stored_as_utc(
    client, login, csrf_token, club, category, activity_state, mutate
):
    """表单填的是本地时间，落库必须转成 UTC。

    这条测试拦的是「显示早 8 小时」那类不报错、不崩溃、只让人跑错场地的缺陷。
    """
    from app.models import Activity

    login(club.student_no)
    resp = post_activity(client, csrf_token, activity_form(category.id))
    activity_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])

    submitted = local_input(96)  # 本地时间字符串
    stored = mutate(lambda s: s.get(Activity, activity_id).start_time)
    assert to_local(stored).strftime("%Y-%m-%dT%H:%M") == submitted


# ---------------------------------------------------------------------------
# 列表：筛选、分页、可见性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["DRAFT", "PENDING", "REJECTED"])
def test_non_public_statuses_are_invisible_to_students(
    client, login, student, club, activity_factory, status
):
    """草稿、待审核、已驳回对普通学生完全不可见。

    活动由社团创建，学生是**局外人**。如果拿学生自己的账号建活动，
    他就成了创建者，本来就应该看得到——那样测的就不是可见性了。
    """
    activity = activity_factory(club.id, title="不该被看到的活动", status=status)

    login(student.student_no)
    plaza = client.get("/activities").get_data(as_text=True)
    assert "不该被看到的活动" not in plaza
    # 不是「看得到但点不动」，是根本拿不到
    assert client.get(f"/activities/{activity.id}").status_code == 404


def test_creator_can_open_their_own_draft(
    client, login, club, activity_factory
):
    activity = activity_factory(club.id, title="校园夜景采风", status=ActivityStatus.DRAFT)
    login(club.student_no)

    resp = client.get(f"/activities/{activity.id}")
    assert resp.status_code == 200
    assert "校园夜景采风" in resp.get_data(as_text=True)


def test_other_club_cannot_open_someone_elses_draft(
    client, login, club, user_factory, activity_factory
):
    stranger = user_factory("club002", "动漫社", "CLUB")
    activity = activity_factory(club.id, title="别人的草稿", status=ActivityStatus.DRAFT)

    login(stranger.student_no)
    assert client.get(f"/activities/{activity.id}").status_code == 404


def test_plaza_filters_by_category_and_keyword(client, login, student, club, category, activity_factory):
    activity_factory(club.id, title="摄影作品点评会", category_id=category.id)
    activity_factory(club.id, title="校园马拉松", category_id=category.id, location="田径场")

    login(student.student_no)
    plaza = client.get("/activities").get_data(as_text=True)
    assert "摄影作品点评会" in plaza and "校园马拉松" in plaza

    filtered = client.get("/activities?keyword=马拉松").get_data(as_text=True)
    assert "校园马拉松" in filtered and "摄影作品点评会" not in filtered

    other = client.get("/activities?category=999999").get_data(as_text=True)
    assert "校园马拉松" not in other


def test_plaza_is_paginated(client, login, student, club, activity_factory):
    """必须分页，不允许一次查出全表。"""
    for index in range(12):
        activity_factory(club.id, title=f"活动-{index:02d}")

    login(student.student_no)
    first = client.get("/activities").get_data(as_text=True)
    second = client.get("/activities?page=2").get_data(as_text=True)

    assert "共 12 场活动" in first
    # 每页 10 条：第一页有「活动-11」，没有「活动-00」；第二页反过来
    assert "活动-11" in first
    assert "活动-01" not in first
    assert "活动-01" in second


def test_plaza_default_sort_is_latest_first(client, login, student, club, activity_factory):
    activity_factory(club.id, title="较早的活动", start_in_hours=10, end_in_hours=12)
    activity_factory(club.id, title="较晚的活动", start_in_hours=100, end_in_hours=102)

    login(student.student_no)
    html = client.get("/activities").get_data(as_text=True)
    assert html.index("较晚的活动") < html.index("较早的活动")


def test_list_card_shows_remaining_seats_and_deadline_state(
    client, login, student, club, activity_factory
):
    """卡片上要直接给出决策信息，别让人点进去才发现报不上。"""
    activity_factory(club.id, title="快满的活动", capacity=3, enrolled_count=3)
    activity_factory(club.id, title="已截止的活动", deadline_in_hours=-1)

    login(student.student_no)
    html = client.get("/activities").get_data(as_text=True)

    assert "名额已满" in html
    assert "报名已截止" in html


# ---------------------------------------------------------------------------
# 编辑与删除
# ---------------------------------------------------------------------------


def test_club_can_edit_own_unpublished_activity(
    client, login, csrf_token, club, category, activity_factory, activity_state
):
    activity = activity_factory(
        club.id, title="旧标题", status=ActivityStatus.DRAFT
    )
    login(club.student_no)

    resp = post_activity(
        client,
        csrf_token,
        activity_form(category.id, title="新标题"),
        path=f"/activities/{activity.id}/edit",
    )
    assert resp.status_code == 302
    assert activity_state(activity.id).title == "新标题"


def test_club_cannot_edit_published_activity(
    client, login, csrf_token, club, category, activity_factory, activity_state
):
    """社团只能改「本人创建且未发布」的活动。"""
    activity = activity_factory(club.id, title="已发布", status=ActivityStatus.PUBLISHED)
    login(club.student_no)

    resp = post_activity(
        client,
        csrf_token,
        activity_form(category.id, title="偷偷改标题"),
        path=f"/activities/{activity.id}/edit",
    )
    assert resp.status_code == 302  # 被拦下并重定向回详情页
    assert activity_state(activity.id).title == "已发布"


def test_club_cannot_edit_someone_elses_activity(
    client, login, csrf_token, club, user_factory, category, activity_factory
):
    stranger = user_factory("club003", "街舞社", "CLUB")
    activity = activity_factory(club.id, title="别人的草稿", status=ActivityStatus.DRAFT)

    login(stranger.student_no)
    resp = post_activity(
        client,
        csrf_token,
        activity_form(category.id, title="篡改"),
        path=f"/activities/{activity.id}/edit",
    )
    assert resp.status_code == 403


def test_admin_can_edit_activity_in_progress_is_locked(
    client, login, csrf_token, admin, category, activity_factory, activity_state
):
    """管理员能改别人的活动，但「进行中」的信息已经锁定。"""
    activity = activity_factory(
        admin.id, title="进行中", status=ActivityStatus.ONGOING,
        start_in_hours=-1, end_in_hours=1, deadline_in_hours=-2,
    )
    login(admin.student_no)

    resp = post_activity(
        client,
        csrf_token,
        activity_form(category.id, title="改不掉"),
        path=f"/activities/{activity.id}/edit",
    )
    assert resp.status_code == 302
    assert activity_state(activity.id).title == "进行中"


def test_capacity_cannot_drop_below_enrolled_count(
    client, login, csrf_token, club, category, activity_factory, activity_state
):
    activity = activity_factory(
        club.id, status=ActivityStatus.DRAFT, capacity=10, enrolled_count=6
    )
    login(club.student_no)

    resp = post_activity(
        client,
        csrf_token,
        activity_form(category.id, capacity="5"),
        path=f"/activities/{activity.id}/edit",
    )
    assert resp.status_code == 400
    assert "人数上限不能小于当前已报名人数（6 人）" in resp.get_data(as_text=True)
    assert activity_state(activity.id).capacity == 10


def test_only_draft_can_be_deleted(
    client, login, csrf_token, club, activity_factory, activity_state
):
    draft = activity_factory(club.id, status=ActivityStatus.DRAFT)
    published = activity_factory(club.id, status=ActivityStatus.PUBLISHED)
    login(club.student_no)

    client.post(
        f"/activities/{draft.id}/delete", data={"csrf_token": csrf_token("/dashboard")}
    )
    assert activity_state(draft.id) is None

    client.post(
        f"/activities/{published.id}/delete", data={"csrf_token": csrf_token("/dashboard")}
    )
    # 已发布的活动删不掉：一旦有人报名，删掉就是毁数据
    assert activity_state(published.id) is not None


# ---------------------------------------------------------------------------
# 状态按时间自动推进
# ---------------------------------------------------------------------------


def test_status_advances_with_time(client, login, admin, activity_factory, activity_state):
    """到点自动推进：PUBLISHED → ONGOING → FINISHED。

    这样就不必为 MVP 引入一套定时任务依赖——状态完全由时间决定，
    读的时候顺手算一次即可。
    """
    starting = activity_factory(
        admin.id, status=ActivityStatus.PUBLISHED,
        start_in_hours=-1, end_in_hours=2, deadline_in_hours=-2,
    )
    ending = activity_factory(
        admin.id, status=ActivityStatus.ONGOING,
        start_in_hours=-3, end_in_hours=-1, deadline_in_hours=-4,
    )
    future = activity_factory(admin.id, status=ActivityStatus.PUBLISHED)

    login(admin.student_no)
    client.get("/activities")  # 任意一次请求都会触发同步

    assert activity_state(starting.id).status == ActivityStatus.ONGOING
    assert activity_state(ending.id).status == ActivityStatus.FINISHED
    assert activity_state(future.id).status == ActivityStatus.PUBLISHED


def test_entering_ongoing_generates_check_in_code(
    client, login, admin, activity_factory, activity_state
):
    activity = activity_factory(
        admin.id, status=ActivityStatus.PUBLISHED,
        start_in_hours=-1, end_in_hours=2, deadline_in_hours=-2,
    )
    assert activity_state(activity.id).check_in_code is None

    login(admin.student_no)
    client.get("/activities")

    code = activity_state(activity.id).check_in_code
    assert code is not None and len(code) == 6 and code.isdigit()


# ---------------------------------------------------------------------------
# 卡片上「剩余名额 · 报名状态」这一段
# ---------------------------------------------------------------------------


def card_meta_lines(html):
    """把每张活动卡片里那一行说明文字的纯文本抠出来。"""
    blocks = re.findall(r'<div class="small mb-3">(.*?)</div>', html, re.S)
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", b)).strip() for b in blocks]


def test_card_meta_never_ends_with_a_dangling_separator(
    client, login, student, club, activity_factory
):
    """活动卡片末尾不能挂着孤零零的「·」。

    ``enroll_closed_note`` 在「已发布、且还没到截止时间」时**什么都不输出**，
    而这恰恰是最常见的情况——每一个还能报名的活动都落在这一支上。模板里原来
    无条件渲染分隔符，于是整页卡片几乎全都显示成「剩余 40 / 40 个名额 ·」，
    多一个没有下文的点号。

    这类问题接口层断言抓不到（HTTP 状态和字段值全对），只有把页面真的渲染出来
    看才会发现——是在浏览器里截图时才看见的。
    """
    activity_factory(club.id, title="还能报名的活动", deadline_in_hours=24)
    activity_factory(club.id, title="已截止的活动", deadline_in_hours=-2)
    activity_factory(
        club.id, title="满员且已截止的活动",
        capacity=5, enrolled_count=5, deadline_in_hours=-2,
    )

    login(student.student_no)
    html = client.get("/activities").get_data(as_text=True)

    lines = card_meta_lines(html)
    assert lines, "没抓到卡片的说明行，选择器可能已经变了"

    for line in lines:
        assert not line.endswith("·"), f"卡片末尾挂着多余的分隔符：{line!r}"

    # 反过来也要成立：有后半段时，分隔符必须正常出现，别为了修 A 把 B 弄坏
    joined = " ".join(lines)
    assert "个名额 · 报名已截止" in joined, f"有截止提示时分隔符反而没了：{joined!r}"
    assert any(line.startswith("名额已满 ·") for line in lines), (
        "满员时的「名额已满 · 报名已截止」这类组合也不能丢："
        f"{[l for l in lines if '名额已满' in l]!r}"
    )

