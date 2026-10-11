"""活动创建与发布、活动列表 / 详情（V2.0）。

覆盖点：

* 教师创建 → **直接发布**（V2.0 没有管理员审核环节）；可以先存草稿；
* 管理员**不能**创建 / 编辑活动（权限矩阵：不代教师编辑内容）；
* 表单校验的每一条规则（含"需审核必须填资格说明"），以及**校验失败不清空已填内容**；
* 列表的筛选、分页、以及「草稿 / 已下线对学生完全不可见」；
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
        "qualification": "",
        "action": "publish",
    }
    data.update(overrides)
    return data


def post_activity(client, csrf_token, data, path="/activities"):
    payload = dict(data)
    payload["csrf_token"] = csrf_token("/dashboard")
    return client.post(path, data=payload)


# ---------------------------------------------------------------------------
# 创建与发布
# ---------------------------------------------------------------------------


def test_teacher_created_activity_is_published_immediately(
    client, login, csrf_token, teacher, category, activity_state
):
    """V2.0：教师创建即发布，不再有"提交给管理员审核"这一步。"""
    login(teacher.student_no)
    resp = post_activity(client, csrf_token, activity_form(category.id))

    assert resp.status_code == 302
    # 302 的 Location 里带着新活动的 id，从地址里把它取出来
    activity_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    assert activity_state(activity_id).status == ActivityStatus.PUBLISHED

    # 发布后立刻出现在活动广场
    plaza = client.get("/activities").get_data(as_text=True)
    assert "手机摄影技巧分享会" in plaza


def test_teacher_can_save_draft_for_later(
    client, login, csrf_token, teacher, category, activity_state
):
    """存草稿：字段可以没填完，先进 DRAFT，不对外展示。"""
    login(teacher.student_no)
    data = activity_form(category.id, action="draft", title="", location="")
    resp = post_activity(client, csrf_token, data)

    activity_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    state = activity_state(activity_id)
    assert state.status == ActivityStatus.DRAFT
    # 数据库里这几个列是 NOT NULL，草稿要有兜底值
    assert state.title


def test_draft_can_be_published_later(
    client, login, csrf_token, teacher, activity_factory, activity_state
):
    """草稿 → 发布：教师自己就是发布者（publish 路由）。"""
    activity = activity_factory(teacher.id, status=ActivityStatus.DRAFT)
    login(teacher.student_no)

    resp = client.post(
        f"/activities/{activity.id}/publish",
        data={"csrf_token": csrf_token("/dashboard")},
    )
    assert resp.status_code == 302
    assert activity_state(activity.id).status == ActivityStatus.PUBLISHED


def test_student_cannot_create_activity(client, login, csrf_token, student, category):
    """学生连表单页都进不去，更不用说提交。"""
    login(student.student_no)
    assert client.get("/activities/new").status_code == 403
    assert post_activity(client, csrf_token, activity_form(category.id)).status_code == 403


def test_admin_cannot_create_or_edit_activities(
    client, login, csrf_token, admin, teacher, category, activity_factory, activity_state
):
    """V2.0 权限矩阵：管理员 ✗ 创建 / 发布 / 修改活动（不代教师编辑内容）。

    管理员的工具是「下线 / 恢复」，不是钻进教师的表单里替人改。
    """
    login(admin.student_no)

    assert client.get("/activities/new").status_code == 403
    assert post_activity(client, csrf_token, activity_form(category.id)).status_code == 403

    # 编辑别人的活动：owner_required 不再对管理员放行
    activity = activity_factory(teacher.id, title="别人的活动", status=ActivityStatus.PUBLISHED)
    resp = post_activity(
        client,
        csrf_token,
        activity_form(category.id, title="管理员改的"),
        path=f"/activities/{activity.id}/edit",
    )
    assert resp.status_code == 403
    assert activity_state(activity.id).title == "别人的活动"


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
        # 勾了「需要审核」却没写资格说明（FR-6.1）
        ({"requires_review": "1", "qualification": ""}, "请填写参加资格说明"),
    ],
)
def test_form_validation_rejects_bad_input(
    client, login, csrf_token, teacher, category, override, expected_message
):
    login(teacher.student_no)
    resp = post_activity(client, csrf_token, activity_form(category.id, **override))

    assert resp.status_code == 400
    assert expected_message in resp.get_data(as_text=True)


def test_review_switch_and_qualification_are_persisted(
    client, login, csrf_token, teacher, category, activity_state
):
    """「需审核 + 资格说明」要落库，详情页要展示——学生报名前就能看到条件。"""
    login(teacher.student_no)
    resp = post_activity(
        client,
        csrf_token,
        activity_form(
            category.id,
            requires_review="1",
            qualification="面向全体在校学生；需自备相机。",
        ),
    )
    activity_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])

    state = activity_state(activity_id)
    assert state.requires_review is True
    assert state.qualification == "面向全体在校学生；需自备相机。"

    detail = client.get(f"/activities/{activity_id}").get_data(as_text=True)
    assert "需审核" in detail
    assert "面向全体在校学生；需自备相机。" in detail


def test_validation_failure_keeps_what_the_user_typed(
    client, login, csrf_token, teacher, category
):
    """校验失败必须保留已填内容——让人从头再填一遍最容易招致放弃。"""
    login(teacher.student_no)
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


def test_unknown_category_is_rejected(client, login, csrf_token, teacher):
    login(teacher.student_no)
    resp = post_activity(client, csrf_token, activity_form(999999))
    assert resp.status_code == 400
    assert "所选分类不存在" in resp.get_data(as_text=True)


def test_start_time_is_stored_as_utc(
    client, login, csrf_token, teacher, category, mutate
):
    """表单填的是本地时间，落库必须转成 UTC。

    这条测试拦的是「显示早 8 小时」那类不报错、不崩溃、只让人跑错场地的缺陷。
    """
    from app.models import Activity

    login(teacher.student_no)
    resp = post_activity(client, csrf_token, activity_form(category.id))
    activity_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])

    submitted = local_input(96)  # 本地时间字符串
    stored = mutate(lambda s: s.get(Activity, activity_id).start_time)
    assert to_local(stored).strftime("%Y-%m-%dT%H:%M") == submitted


# ---------------------------------------------------------------------------
# 列表：筛选、分页、可见性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["DRAFT", "OFFLINE"])
def test_non_public_statuses_are_invisible_to_students(
    client, login, student, teacher, activity_factory, status
):
    """草稿、**已下线**对普通学生完全不可见。

    活动由教师创建，学生是**局外人**。如果拿学生自己的账号建活动，
    他就成了创建者，本来就应该看得到——那样测的就不是可见性了。
    """
    activity = activity_factory(teacher.id, title="不该被看到的活动", status=status)

    login(student.student_no)
    plaza = client.get("/activities").get_data(as_text=True)
    assert "不该被看到的活动" not in plaza
    # 不是「看得到但点不动」，是根本拿不到
    assert client.get(f"/activities/{activity.id}").status_code == 404


def test_offlined_activity_still_visible_to_organizer_and_admin(
    client, login, teacher, admin, activity_factory
):
    """TC-14：被下线的活动，教师端与管理员仍可见（含处理标记）。"""
    activity = activity_factory(teacher.id, title="被下线的活动", status=ActivityStatus.OFFLINE)

    login(teacher.student_no)
    assert client.get(f"/activities/{activity.id}").status_code == 200
    assert "已被管理员下线" in client.get(f"/activities/{activity.id}").get_data(as_text=True)

    login(admin.student_no)
    assert client.get(f"/activities/{activity.id}").status_code == 200


def test_creator_can_open_their_own_draft(
    client, login, teacher, activity_factory
):
    activity = activity_factory(teacher.id, title="校园夜景采风", status=ActivityStatus.DRAFT)
    login(teacher.student_no)

    resp = client.get(f"/activities/{activity.id}")
    assert resp.status_code == 200
    assert "校园夜景采风" in resp.get_data(as_text=True)


def test_other_teacher_cannot_open_someone_elses_draft(
    client, login, teacher, other_teacher, activity_factory
):
    activity = activity_factory(teacher.id, title="别人的草稿", status=ActivityStatus.DRAFT)

    login(other_teacher.student_no)
    assert client.get(f"/activities/{activity.id}").status_code == 404


def test_plaza_filters_by_category_and_keyword(client, login, student, teacher, category, activity_factory):
    activity_factory(teacher.id, title="摄影作品点评会", category_id=category.id)
    activity_factory(teacher.id, title="校园马拉松", category_id=category.id, location="田径场")

    login(student.student_no)
    plaza = client.get("/activities").get_data(as_text=True)
    assert "摄影作品点评会" in plaza and "校园马拉松" in plaza

    filtered = client.get("/activities?keyword=马拉松").get_data(as_text=True)
    assert "校园马拉松" in filtered and "摄影作品点评会" not in filtered

    other = client.get("/activities?category=999999").get_data(as_text=True)
    assert "校园马拉松" not in other


def test_plaza_is_paginated(client, login, student, teacher, activity_factory):
    """必须分页，不允许一次查出全表。"""
    for index in range(12):
        activity_factory(teacher.id, title=f"活动-{index:02d}")

    login(student.student_no)
    first = client.get("/activities").get_data(as_text=True)
    second = client.get("/activities?page=2").get_data(as_text=True)

    assert "共 12 场活动" in first
    # 每页 10 条：第一页有「活动-11」，没有「活动-01」；第二页反过来
    assert "活动-11" in first
    assert "活动-01" not in first
    assert "活动-01" in second


def test_plaza_default_sort_is_latest_first(client, login, student, teacher, activity_factory):
    activity_factory(teacher.id, title="较早的活动", start_in_hours=10, end_in_hours=12)
    activity_factory(teacher.id, title="较晚的活动", start_in_hours=100, end_in_hours=102)

    login(student.student_no)
    html = client.get("/activities").get_data(as_text=True)
    assert html.index("较晚的活动") < html.index("较早的活动")


def test_list_card_shows_enroll_state_marker(
    client, login, student, teacher, activity_factory
):
    """FR-1.1：列表里能一眼区分「可报名 / 已满员·可候补」。

    卡片上要直接给出决策信息，别让人点进去才发现报不上。
    """
    activity_factory(teacher.id, title="还能报名的活动", capacity=10, enrolled_count=3)
    activity_factory(teacher.id, title="满员的活动", capacity=3, enrolled_count=3)
    activity_factory(teacher.id, title="已截止的活动", deadline_in_hours=-1)

    login(student.student_no)
    html = client.get("/activities").get_data(as_text=True)

    assert "可报名" in html
    assert "已满员 · 可候补" in html
    assert "报名已截止" in html


# ---------------------------------------------------------------------------
# 编辑与删除
# ---------------------------------------------------------------------------


def test_teacher_can_edit_own_draft(
    client, login, csrf_token, teacher, category, activity_factory, activity_state
):
    activity = activity_factory(
        teacher.id, title="旧标题", status=ActivityStatus.DRAFT
    )
    login(teacher.student_no)

    resp = post_activity(
        client,
        csrf_token,
        activity_form(category.id, title="新标题"),
        path=f"/activities/{activity.id}/edit",
    )
    assert resp.status_code == 302
    assert activity_state(activity.id).title == "新标题"


def test_teacher_can_edit_own_published_activity(
    client, login, csrf_token, teacher, category, activity_factory, activity_state
):
    """FR-7.2：时间、地点等有变化时可及时调整——**已发布**的活动也能改。

    这是 V2.0 相对 V1.0 的一个明确放松：V1.0 里「社团发布后不能再改」，
    但访谈里教师明确要求能改（T-02）。
    """
    activity = activity_factory(teacher.id, title="已发布", status=ActivityStatus.PUBLISHED)
    login(teacher.student_no)

    resp = post_activity(
        client,
        csrf_token,
        activity_form(category.id, title="改好的标题", location="新场地"),
        path=f"/activities/{activity.id}/edit",
    )
    assert resp.status_code == 302
    assert activity_state(activity.id).title == "改好的标题"


def test_teacher_cannot_edit_someone_elses_activity(
    client, login, csrf_token, teacher, other_teacher, category, activity_factory
):
    activity = activity_factory(teacher.id, title="别人的草稿", status=ActivityStatus.DRAFT)

    login(other_teacher.student_no)
    resp = post_activity(
        client,
        csrf_token,
        activity_form(category.id, title="篡改"),
        path=f"/activities/{activity.id}/edit",
    )
    assert resp.status_code == 403


def test_ongoing_activity_is_locked_for_editing(
    client, login, csrf_token, teacher, category, activity_factory, activity_state
):
    """活动一旦开始（进行中），信息整体锁定。

    报名名单、签到都以当时的信息为准；边办边改活动信息只会让人跑空场地。
    """
    activity = activity_factory(
        teacher.id, title="进行中", status=ActivityStatus.ONGOING,
        start_in_hours=-1, end_in_hours=1, deadline_in_hours=-2,
    )
    login(teacher.student_no)

    resp = post_activity(
        client,
        csrf_token,
        activity_form(category.id, title="改不掉"),
        path=f"/activities/{activity.id}/edit",
    )
    assert resp.status_code == 302
    assert activity_state(activity.id).title == "进行中"


def test_capacity_cannot_drop_below_enrolled_count(
    client, login, csrf_token, teacher, category, activity_factory, activity_state
):
    activity = activity_factory(
        teacher.id, status=ActivityStatus.DRAFT, capacity=10, enrolled_count=6
    )
    login(teacher.student_no)

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
    client, login, csrf_token, teacher, activity_factory, activity_state
):
    draft = activity_factory(teacher.id, status=ActivityStatus.DRAFT)
    published = activity_factory(teacher.id, status=ActivityStatus.PUBLISHED)
    login(teacher.student_no)

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


def test_status_advances_with_time(client, login, teacher, activity_factory, activity_state):
    """到点自动推进：PUBLISHED → ONGOING → FINISHED。

    这样就不必引入一套定时任务依赖——状态完全由时间决定，
    读的时候顺手算一次即可。
    """
    starting = activity_factory(
        teacher.id, status=ActivityStatus.PUBLISHED,
        start_in_hours=-1, end_in_hours=2, deadline_in_hours=-2,
    )
    ending = activity_factory(
        teacher.id, status=ActivityStatus.ONGOING,
        start_in_hours=-3, end_in_hours=-1, deadline_in_hours=-4,
    )
    future = activity_factory(teacher.id, status=ActivityStatus.PUBLISHED)

    login(teacher.student_no)
    client.get("/activities")  # 任意一次请求都会触发同步

    assert activity_state(starting.id).status == ActivityStatus.ONGOING
    assert activity_state(ending.id).status == ActivityStatus.FINISHED
    assert activity_state(future.id).status == ActivityStatus.PUBLISHED


def test_offlined_activity_does_not_advance_with_time(
    client, login, teacher, activity_factory, activity_state
):
    """被下线的活动不参与时间推进：它已经不在时间线上（恢复时再重新定位）。"""
    activity = activity_factory(
        teacher.id, status=ActivityStatus.OFFLINE,
        start_in_hours=-1, end_in_hours=2, deadline_in_hours=-2,
    )

    login(teacher.student_no)
    client.get("/activities")

    assert activity_state(activity.id).status == ActivityStatus.OFFLINE


def test_entering_ongoing_generates_check_in_code(
    client, login, teacher, activity_factory, activity_state
):
    activity = activity_factory(
        teacher.id, status=ActivityStatus.PUBLISHED,
        start_in_hours=-1, end_in_hours=2, deadline_in_hours=-2,
    )
    assert activity_state(activity.id).check_in_code is None

    login(teacher.student_no)
    client.get("/activities")

    code = activity_state(activity.id).check_in_code
    assert code is not None and len(code) == 6 and code.isdigit()


# ---------------------------------------------------------------------------
# 卡片上「报名状态」这一段
# ---------------------------------------------------------------------------


def card_meta_lines(html):
    """把每张活动卡片里那一行说明文字的纯文本抠出来。"""
    blocks = re.findall(r'<div class="small mb-3">(.*?)</div>', html, re.S)
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", b)).strip() for b in blocks]


def test_card_meta_never_ends_with_a_dangling_separator(
    client, login, student, teacher, activity_factory
):
    """活动卡片末尾不能挂着孤零零的「·」。

    这条是从 V1.0 留下来的回归：当年「剩余名额」和「截止提示」用「·」拼接、
    分隔符无条件渲染，而"还能报名"恰恰是最常见的情况，于是整页卡片几乎全都
    显示成「剩余 40 / 40 个名额 ·」，多一个没有下文的点号。

    V2.0 把这一行改成由 `enroll_line` 宏统一渲染，保证**永远有内容**。
    这类问题接口层断言抓不到（HTTP 状态和字段值全对），只有把页面真的渲染
    出来看才会发现。
    """
    activity_factory(teacher.id, title="还能报名的活动", deadline_in_hours=24)
    activity_factory(teacher.id, title="已截止的活动", deadline_in_hours=-2)
    activity_factory(
        teacher.id, title="满员且已截止的活动",
        capacity=5, enrolled_count=5, deadline_in_hours=-2,
    )
    activity_factory(
        teacher.id, title="满员但还能候补的活动",
        capacity=5, enrolled_count=5, deadline_in_hours=24,
    )

    login(student.student_no)
    html = client.get("/activities").get_data(as_text=True)

    lines = card_meta_lines(html)
    assert lines, "没抓到卡片的说明行，选择器可能已经变了"

    for line in lines:
        assert not line.endswith("·"), f"卡片末尾挂着多余的分隔符：{line!r}"

    joined = " ".join(lines)
    assert "可报名" in joined, f"可报名的卡片上没有「可报名」标记（FR-1.1）：{joined!r}"
    assert "已满员 · 可候补" in joined, (
        f"满员但还能候补的卡片标记不对（FR-1.1）：{joined!r}"
    )
    assert "报名已截止" in joined


# ---------------------------------------------------------------------------
# 静态资源本地化：Bootstrap 不依赖外网 CDN
# ---------------------------------------------------------------------------


def test_bootstrap_is_served_locally(client):
    """页面的 Bootstrap 必须来自本地 static，不能引外网 CDN。

    校园网与演示环境经常连不上外域：取不到样式时页面"白底裸奔"——
    功能都对，但给人看的第一印象是"这系统坏了"。这条测试防止有人
    顺手把 CDN 链接改回去。
    """
    resp = client.get("/static/vendor/bootstrap.min.css")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Bootstrap" in body[:300], "拿到的不是 Bootstrap 样式文件"
    assert "--bs-body-" in body, "样式文件不完整"

    assert client.get("/static/vendor/bootstrap.bundle.min.js").status_code == 200

    base = open("app/templates/base.html", encoding="utf-8").read()
    for probe in ("cdn.jsdelivr.net", "unpkg.com", "cdn.bootcdn.net", "staticfile.net"):
        assert probe not in base, f"base.html 又引用了外部 CDN：{probe}"
