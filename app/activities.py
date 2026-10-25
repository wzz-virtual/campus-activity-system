"""活动：创建、编辑、发布、列表、详情，以及报名 / 候补 / 审核 / 签到的入口。

路由分两类：

* **页面路由**（GET）—— 返回渲染好的 HTML；
* **动作路由**（POST）—— 接收表单，处理完**重定向回页面**，即 Post/Redirect/Get。
  直接返回渲染结果会导致用户一刷新就重复提交一次（取消报名这种操作重复执行
  的后果是实打实的）。重定向之后，浏览器地址栏是一个普通 GET 地址，
  刷新只会重新拉一次页面。

权限的做法统一为：装饰器拦第一道（角色 / 归属权），视图里再按**状态**拦第二道。
前端把按钮藏起来不算权限控制——改一个请求就能绕过。

V2.0 的变化：
* 发布不再经过管理员审核（`publish` 直接由组织教师完成）；
* 新增报名审核（教师对本人活动的「待审核」申请逐条处理）；
* 名单页分成「待审核 / 正式 / 候补」三段。
"""

import time
from datetime import timedelta

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from .extensions import db
from . import metrics
from .models import (
    Activity,
    ActivityStatus,
    Category,
    EnrollStatus,
    Enrollment,
    Role,
    utcnow,
)
from .permissions import owner_required, role_required
from .services import (
    CANCEL_LOCK_HOURS,
    CHECK_IN_STATE_LABELS,
    CHECK_IN_STATE_STYLES,
    ENROLL_STATUS_STYLES,
    activity_heat,
    activity_stats,
    cancel_activity as cancel_activity_service,
    cancel_blocked_reason,
    cancel_deadline_text,
    cancel_enrollment,
    can_edit_activity,
    capacity_hints,
    check_in_by_code,
    check_in_state,
    collect_activity_form,
    edit_blocked_reason,
    enroll,
    enroll_allowed_reason,
    enroll_blocked_reason,
    find_time_conflicts,
    latest_offline_record,
    latest_offline_records,
    open_check_in as open_check_in_service,
    organizer_overview,
    pending_count,
    promote_waitlist,
    publish_activity,
    review_enrollment,
    set_check_in,
    validate_activity_form,
    waitlist_count,
    waitlist_opportunity,
    waitlist_position,
    waitlist_query,
)
from .timeutils import format_local_input, parse_local, utc_iso

bp = Blueprint("activities", __name__)

# 活动列表每页条数
PAGE_SIZE = 10


# ---------------------------------------------------------------------------
# 取数据的公共入口
# ---------------------------------------------------------------------------


def get_activity_or_404(activity_id):
    """按 id 取活动，取不到就 404。

    ⚠️ 不存在时**必须** 404 而不是返回 None。返回 None 会让上游的
    归属权校验变成 403，等于告诉对方「这个 id 存在，只是不归你」——
    403 和 404 的区别本身就可以被用来探测数据。
    """
    activity = db.session.get(Activity, activity_id)
    if activity is None:
        abort(404)
    return activity


def visible_activity_or_404(activity_id):
    """按可见性取活动。

    普通学生只能看到 `已发布 / 进行中 / 已结束 / 已取消` 这四种状态。
    草稿、**已下线**对创建者和管理员以外的人**完全不可见**——
    不是"看得到但点不动"，是根本拿不到。
    """
    activity = get_activity_or_404(activity_id)
    if activity.status in ActivityStatus.VISIBLE_TO_STUDENT:
        return activity
    if current_user.is_authenticated and (
        current_user.is_admin or activity.organizer_id == current_user.id
    ):
        return activity
    abort(404)


def _my_enrollment_map(activity_ids):
    """当前用户在这批活动里的报名记录，键是活动 id。

    一次性查出来，避免在列表循环里逐个查（那会变成 N+1 次查询）。
    """
    if not current_user.is_authenticated or not activity_ids:
        return {}
    rows = (
        db.session.execute(
            db.select(Enrollment).where(
                Enrollment.user_id == current_user.id,
                Enrollment.activity_id.in_(activity_ids),
            )
        )
        .scalars()
        .all()
    )
    return {row.activity_id: row for row in rows}


def _categories():
    return (
        db.session.execute(
            db.select(Category).order_by(Category.sort_order, Category.id)
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# 活动广场：列表
# ---------------------------------------------------------------------------


@bp.get("/activities")
def list_activities():
    """活动广场。

    * 只展示对学生可见的状态（**被管理员下线的活动不在其中**）；
    * 支持分类 / 状态 / 关键词筛选；
    * **必须分页**，不允许一次查出全表。
    """
    page = request.args.get("page", type=int) or 1
    category_id = request.args.get("category", type=int)
    status = request.args.get("status") or ""
    keyword = (request.args.get("keyword") or "").strip()
    # DR-7：日期 / 周末 / 报名状态三个新筛选
    date_str = (request.args.get("date") or "").strip()
    weekend = request.args.get("weekend") == "1"
    enroll_state = request.args.get("enroll_state") or ""

    now = utcnow()

    condition = db.select(Activity).where(
        Activity.status.in_(ActivityStatus.VISIBLE_TO_STUDENT)
    )
    if category_id:
        condition = condition.where(Activity.category_id == category_id)
    if status and status in ActivityStatus.VISIBLE_TO_STUDENT:
        condition = condition.where(Activity.status == status)
    else:
        status = ""
    if keyword:
        # 标题 / 简介 / 地点三处一起搜
        pattern = f"%{keyword}%"
        condition = condition.where(
            db.or_(
                Activity.title.like(pattern),
                Activity.description.like(pattern),
                Activity.location.like(pattern),
            )
        )
    if date_str:
        # 按**本地日期**筛（库里存 UTC）。解析失败就当没填，不报错——
        # 手改 URL 参数不该让整页 500。
        try:
            day_start = parse_local(f"{date_str} 00:00")
        except ValueError:
            day_start = None
        if day_start is not None:
            condition = condition.where(
                Activity.start_time >= day_start,
                Activity.start_time < day_start + timedelta(days=1),
            )
    if weekend:
        # SQLite：把 UTC 转成本地（+8）再取星期几，0=周日、6=周六
        weekday = db.func.strftime(
            "%w", db.func.datetime(Activity.start_time, "+8 hours")
        )
        condition = condition.where(weekday.in_(("0", "6")))
    if enroll_state == "open":
        condition = condition.where(
            Activity.status == ActivityStatus.PUBLISHED,
            Activity.enroll_deadline > now,
            Activity.enrolled_count < Activity.capacity,
        )
    elif enroll_state == "waitlist":
        condition = condition.where(
            Activity.status == ActivityStatus.PUBLISHED,
            Activity.enroll_deadline > now,
            Activity.enrolled_count >= Activity.capacity,
        )
    else:
        enroll_state = ""

    # 默认按开始时间倒序：最新的活动排在前面
    pagination = db.paginate(
        condition.order_by(Activity.start_time.desc()),
        page=page,
        per_page=PAGE_SIZE,
        error_out=False,
    )
    activities = pagination.items
    my_enrollments = _my_enrollment_map([a.id for a in activities])

    return render_template(
        "activities/list.html",
        pagination=pagination,
        activities=activities,
        categories=_categories(),
        my_enrollments=my_enrollments,
        # 候补排位只在「我的报名」里算一次太零散，这里按需补上
        waitlist_positions={
            activity_id: waitlist_position(enrollment)
            for activity_id, enrollment in my_enrollments.items()
            if enrollment.status == EnrollStatus.WAITLISTED
        },
        current_category=category_id,
        current_status=status,
        keyword=keyword,
        # DR-7 的三个新筛选条件（回填到表单 / 分页链接）
        current_date=date_str,
        weekend=weekend,
        enroll_state=enroll_state,
        # DR-12：热度标签（填充率 + 余位口径）
        heat={a.id: activity_heat(a, now) for a in activities},
        now=now,
        status_labels=ActivityStatus.LABELS,
    )


# ---------------------------------------------------------------------------
# 创建 / 编辑
# ---------------------------------------------------------------------------


def _form_context(activity, raw, errors):
    return {
        "activity": activity,
        "raw": raw,
        "errors": errors,
        "categories": _categories(),
        "is_new": activity is None,
        # DR-12：同类活动的容量参考，帮教师定人数上限
        "capacity_hints": capacity_hints(),
    }


@bp.get("/activities/new")
@role_required(Role.TEACHER)
def new_activity():
    categories = _categories()
    if not categories:
        flash("还没有任何活动分类，请先让管理员创建分类。", "warning")
        return redirect(url_for("activities.list_activities"))

    raw = {name: "" for name in collect_activity_form({})}
    return render_template(
        "activities/form.html", **_form_context(None, raw, {})
    )


def _apply_form(activity, parsed):
    """把校验通过的值写进活动对象。"""
    for name in (
        "title",
        "category_id",
        "cover_url",
        "description",
        "location",
        "capacity",
        "start_time",
        "end_time",
        "enroll_deadline",
        "requires_review",
        "qualification",
    ):
        setattr(activity, name, parsed[name])


@bp.post("/activities")
@role_required(Role.TEACHER)
def create_activity():
    """创建活动。

    两种提交方式：

    * ``action=draft`` —— 存草稿，字段可以先不填全；
    * 直接提交 —— **创建即发布**（V2.0 没有管理员审核环节，教师自己就是
      发布者，事后由管理员监督）。
    """
    raw = collect_activity_form(request.form)
    categories = _categories()
    errors, parsed = validate_activity_form(raw, {c.id for c in categories})

    wants_draft = request.form.get("action") == "draft"

    if errors and not wants_draft:
        flash(f"表单里有 {len(errors)} 处需要修正", "danger")
        # 关键：把用户填好的内容原样带回去，只标出错误项。
        # 让人从头再填一遍是最容易招致放弃的体验。
        return render_template(
            "activities/form.html", **_form_context(None, raw, errors)
        ), 400

    activity = Activity(organizer_id=current_user.id, enrolled_count=0)
    if wants_draft:
        # 草稿允许字段不完整，所以用「缺什么补什么」的默认值兜底，
        # 强制校验留到发布那一步再做。
        _apply_form(activity, _draft_defaults(parsed))
        activity.status = ActivityStatus.DRAFT
        flash("已存为草稿，可以稍后继续编辑或发布。", "success")
    else:
        _apply_form(activity, parsed)
        activity.status = ActivityStatus.PUBLISHED
        flash("活动已发布，学生现在就能在活动广场看到它。", "success")

    db.session.add(activity)
    db.session.commit()

    return redirect(url_for("activities.activity_detail", activity_id=activity.id))


def _draft_defaults(parsed):
    """草稿的兜底值。

    数据库里这些列都是 NOT NULL，但草稿本来就允许没填完，
    所以缺的字段先塞一个占位值，等真正发布时再要求填全。
    """
    filled = {
        "title": parsed.get("title") or "（未命名草稿）",
        "category_id": parsed.get("category_id"),
        "cover_url": parsed.get("cover_url"),
        "description": parsed.get("description"),
        "location": parsed.get("location") or "待定",
        "capacity": parsed.get("capacity") or 1,
        "start_time": parsed.get("start_time"),
        "end_time": parsed.get("end_time"),
        "enroll_deadline": parsed.get("enroll_deadline"),
        "requires_review": parsed.get("requires_review", False),
        "qualification": parsed.get("qualification"),
    }
    fallback = utcnow()
    for name in ("start_time", "end_time", "enroll_deadline"):
        if filled[name] is None:
            filled[name] = fallback
    # 草稿也得让「结束晚于开始、截止不晚于开始」这两条成立，
    # 否则连状态机同步（按时间推进状态）都会算出奇怪的结果
    if filled["end_time"] <= filled["start_time"]:
        filled["end_time"] = filled["start_time"] + timedelta(hours=1)
    if filled["enroll_deadline"] > filled["start_time"]:
        filled["enroll_deadline"] = filled["start_time"]
    if filled["category_id"] is None:
        filled["category_id"] = db.session.execute(
            db.select(Category.id).order_by(Category.sort_order, Category.id)
        ).scalars().first()
    return filled


@bp.get("/activities/<int:activity_id>/edit")
@owner_required(lambda activity_id: get_activity_or_404(activity_id).organizer_id)
def edit_activity(activity_id):
    activity = get_activity_or_404(activity_id)
    reason = edit_blocked_reason(current_user, activity)
    if reason:
        flash(reason, "warning")
        return redirect(url_for("activities.activity_detail", activity_id=activity_id))

    raw = {
        "title": activity.title,
        "category_id": str(activity.category_id or ""),
        "cover_url": activity.cover_url or "",
        "description": activity.description or "",
        "start_time": format_local_input(activity.start_time),
        "end_time": format_local_input(activity.end_time),
        "enroll_deadline": format_local_input(activity.enroll_deadline),
        "location": activity.location or "",
        "capacity": str(activity.capacity),
        "qualification": activity.qualification or "",
        "requires_review": "1" if activity.requires_review else "",
    }
    return render_template("activities/form.html", **_form_context(activity, raw, {}))


@bp.post("/activities/<int:activity_id>/edit")
@owner_required(lambda activity_id: get_activity_or_404(activity_id).organizer_id)
def update_activity(activity_id):
    activity = get_activity_or_404(activity_id)
    reason = edit_blocked_reason(current_user, activity)
    if reason:
        flash(reason, "warning")
        return redirect(url_for("activities.activity_detail", activity_id=activity_id))

    raw = collect_activity_form(request.form)
    errors, parsed = validate_activity_form(
        raw, {c.id for c in _categories()}, capacity_floor=activity.enrolled_count
    )

    if errors:
        flash("表单里有 " + str(len(errors)) + " 处需要修正", "danger")
        return render_template(
            "activities/form.html", **_form_context(activity, raw, errors)
        ), 400

    _apply_form(activity, parsed)
    # 上限可能被调大——多出来的余位要立刻喂给候补队列，
    # 否则会出现「明明有空位、队列里还有人等」的违约状态（BR-6）。
    promote_waitlist(activity.id)
    db.session.commit()
    flash("活动信息已更新。", "success")
    return redirect(url_for("activities.activity_detail", activity_id=activity.id))


@bp.post("/activities/<int:activity_id>/delete")
@owner_required(lambda activity_id: get_activity_or_404(activity_id).organizer_id)
def delete_activity(activity_id):
    """删除草稿。只有草稿能删——已发布的活动一旦有人报名，删掉就是毁数据。"""
    activity = get_activity_or_404(activity_id)
    if activity.status != ActivityStatus.DRAFT:
        flash("只有草稿可以删除。已发布的活动请使用「取消活动」。", "warning")
        return redirect(url_for("activities.activity_detail", activity_id=activity_id))

    db.session.delete(activity)
    db.session.commit()
    flash("草稿已删除。", "info")
    return redirect(url_for("activities.my_activities"))


# ---------------------------------------------------------------------------
# 详情
# ---------------------------------------------------------------------------


@bp.get("/activities/<int:activity_id>")
def activity_detail(activity_id):
    activity = visible_activity_or_404(activity_id)

    enrollment = None
    if current_user.is_authenticated:
        enrollment = db.session.execute(
            db.select(Enrollment).where(
                Enrollment.activity_id == activity.id,
                Enrollment.user_id == current_user.id,
            )
        ).scalar_one_or_none()

    now = utcnow()
    enrollment_active = enrollment is not None and enrollment.is_active

    blocked_reason = None
    can_enroll = False
    enroll_action = None
    if not current_user.is_authenticated:
        blocked_reason = "请先登录后再报名"
    elif enrollment_active:
        blocked_reason = None
    else:
        # 这里只是**提前告知**按钮状态。真正的判断在 services.enroll() 里，
        # 提交时还会在数据库层再判一次——前端提示不能当作校验。
        blocked_reason = enroll_blocked_reason(activity.id, now)
        can_enroll = blocked_reason is None
        if can_enroll:
            # 按钮文案按「会走到哪条路径」来给，别让人点了才知道结果：
            # 需审核 → 提交申请；满员 → 加入候补；否则 → 直接报名
            if activity.requires_review:
                enroll_action = "apply"
            elif activity.is_full:
                enroll_action = "waitlist"
            else:
                enroll_action = "enroll"

    cancel_reason = None
    cancel_until = None
    position = None
    if enrollment_active:
        cancel_reason = cancel_blocked_reason(activity, enrollment, now)
        if enrollment.status == EnrollStatus.CONFIRMED:
            # 只有正式报名有「取消截止」的概念（名单锁定窗口）
            cancel_until = cancel_deadline_text(activity) if cancel_reason is None else None
        elif enrollment.status == EnrollStatus.WAITLISTED:
            position = waitlist_position(enrollment)

    is_organizer = (
        current_user.is_authenticated and activity.organizer_id == current_user.id
    )

    # DR-11：只有「还能报名」时才需要算时间冲突，避免无谓查询
    conflicts = []
    if current_user.is_authenticated and can_enroll:
        conflicts = find_time_conflicts(current_user.id, activity)

    # DR-4：满员的活动才需要在报名区给「还值不值得等」的参考
    opportunity = waitlist_opportunity(activity.id) if activity.is_full else None

    return render_template(
        "activities/detail.html",
        activity=activity,
        enrollment=enrollment,
        enrollment_active=enrollment_active,
        can_enroll=can_enroll,
        enroll_action=enroll_action,
        blocked_reason=blocked_reason,
        cancel_reason=cancel_reason,
        cancel_until=cancel_until,
        waitlist_position=position,
        check_in_state=check_in_state(activity, enrollment, now)
        if enrollment_active and enrollment.status == EnrollStatus.CONFIRMED
        else None,
        check_in_state_labels=CHECK_IN_STATE_LABELS,
        check_in_state_styles=CHECK_IN_STATE_STYLES,
        enroll_status_styles=ENROLL_STATUS_STYLES,
        stats=activity_stats(activity) if is_organizer else None,
        can_edit=current_user.is_authenticated
        and can_edit_activity(current_user, activity),
        is_organizer=is_organizer,
        offline_record=latest_offline_record(activity.id)
        if activity.status == ActivityStatus.OFFLINE
        else None,
        offinable_statuses=ActivityStatus.OFFINABLE,
        status_labels=ActivityStatus.LABELS,
        # V3.0：时间冲突提示（DR-11）、候补机会参考（DR-4）、热度（DR-12）
        conflicts=conflicts,
        waitlist_opportunity=opportunity,
        heat=activity_heat(activity, now),
        now=now,
    )


# ---------------------------------------------------------------------------
# 状态流转
# ---------------------------------------------------------------------------


@bp.post("/activities/<int:activity_id>/publish")
@owner_required(lambda activity_id: get_activity_or_404(activity_id).organizer_id)
def publish(activity_id):
    activity = get_activity_or_404(activity_id)
    ok, message = publish_activity(activity, current_user.id)
    flash(message or "活动已发布。", "success" if ok else "warning")
    return redirect(url_for("activities.activity_detail", activity_id=activity_id))


@bp.post("/activities/<int:activity_id>/cancel")
@owner_required(lambda activity_id: get_activity_or_404(activity_id).organizer_id)
def cancel_activity(activity_id):
    activity = get_activity_or_404(activity_id)
    ok, message = cancel_activity_service(
        activity, current_user.id, request.form.get("reason")
    )
    flash(message or "活动已取消。", "info" if ok else "warning")
    return redirect(url_for("activities.activity_detail", activity_id=activity_id))


# ---------------------------------------------------------------------------
# 报名 / 取消 / 退出候补 / 撤销申请
# ---------------------------------------------------------------------------


@bp.post("/activities/<int:activity_id>/enroll")
@login_required
def enroll_activity(activity_id):
    """报名。结果是哪一种（正式 / 候补 / 待审核），由业务层决定，
    提示语直接用它返回的文案——三种结果的语气完全不同。

    V3.0 在这里补两件事：

    * **DR-11 时间冲突二次确认**：与本人已有的正式活动同日 / 时间重叠时，
      先返回确认页；用户点「仍然报名」（表单带 ``confirm=1``）才真正提交。
      不强制拦截——冲突只是提示。
    * **DR-5 指标采集**：记录本次报名请求的耗时，供管理端指标看板观测峰值。
    """
    started = time.perf_counter()
    activity = get_activity_or_404(activity_id)

    if request.form.get("confirm") != "1":
        conflicts = find_time_conflicts(current_user.id, activity)
        if conflicts:
            return render_template(
                "activities/conflict_confirm.html",
                activity=activity,
                conflicts=conflicts,
            )

    ok, message = enroll(activity_id, current_user.id)
    metrics.record((time.perf_counter() - started) * 1000, ok)

    if ok:
        flash(message, "success")
    elif "无需重复提交" in message:
        # DR-2：重复提交不是错误，别用警告色吓人
        flash(message, "info")
    else:
        flash(message, "warning")
    return redirect(url_for("activities.activity_detail", activity_id=activity_id))


@bp.post("/activities/<int:activity_id>/unenroll")
@login_required
def unenroll_activity(activity_id):
    """取消报名 / 退出候补 / 撤销申请，统一走这一个入口。"""
    get_activity_or_404(activity_id)
    ok, message = cancel_enrollment(activity_id, current_user.id)
    if ok:
        flash("已取消，名额已释放给候补队列。", "info")
    else:
        flash(message, "warning")
    return redirect(url_for("activities.activity_detail", activity_id=activity_id))


@bp.get("/my/enrollments")
@login_required
def my_enrollments():
    """我的报名。逐条给出状态（含候补排位、审核时间），并把
    「还能不能取消 / 退出」和原因写清楚。"""
    now = utcnow()
    enrollments = (
        db.session.execute(
            db.select(Enrollment)
            .where(Enrollment.user_id == current_user.id)
            .order_by(Enrollment.enrolled_at.desc())
        )
        .scalars()
        .all()
    )

    rows = []
    for enrollment in enrollments:
        activity = enrollment.activity
        active = enrollment.is_active
        rows.append(
            {
                "enrollment": enrollment,
                "activity": activity,
                "state": check_in_state(activity, enrollment, now)
                if enrollment.status == EnrollStatus.CONFIRMED
                else None,
                "waitlist_position": waitlist_position(enrollment)
                if enrollment.status == EnrollStatus.WAITLISTED
                else None,
                "cancel_reason": cancel_blocked_reason(activity, enrollment, now)
                if active
                else None,
                # 还能取消时，明确写出可以反悔到什么时候（只对正式有意义）
                "cancel_until": cancel_deadline_text(activity)
                if enrollment.status == EnrollStatus.CONFIRMED
                else None,
                # 已拒绝 / 已取消之后，条件允许就能重新报名（DEC-3）
                "can_reapply": (not active)
                and enroll_allowed_reason(activity, now) is None,
            }
        )

    return render_template(
        "activities/my_enrollments.html",
        rows=rows,
        state_labels=CHECK_IN_STATE_LABELS,
        state_styles=CHECK_IN_STATE_STYLES,
        enroll_status_styles=ENROLL_STATUS_STYLES,
        cancel_lock_hours=CANCEL_LOCK_HOURS,
    )


# ---------------------------------------------------------------------------
# 我的活动（组织教师视角）
# ---------------------------------------------------------------------------


@bp.get("/my/activities")
@role_required(Role.TEACHER)
def my_activities():
    """我的活动。按状态分组，并把「哪些活动有申请等着处理」标出来。"""
    activities = (
        db.session.execute(
            db.select(Activity)
            .where(Activity.organizer_id == current_user.id)
            .order_by(Activity.start_time.desc())
        )
        .scalars()
        .all()
    )

    groups = {}
    for activity in activities:
        groups.setdefault(activity.status, []).append(activity)

    ordered_groups = [
        (status, ActivityStatus.LABELS[status], groups[status])
        for status in ActivityStatus.ALL
        if status in groups
    ]

    return render_template(
        "activities/my_activities.html",
        groups=ordered_groups,
        total=len(activities),
        status_labels=ActivityStatus.LABELS,
        pending_counts={a.id: pending_count(a.id) for a in activities},
        waitlist_counts={a.id: waitlist_count(a.id) for a in activities},
        offline_records=latest_offline_records([a.id for a in activities]),
    )


# ---------------------------------------------------------------------------
# 签到（学生自助）
# ---------------------------------------------------------------------------


@bp.route("/activities/<int:activity_id>/check-in", methods=["GET", "POST"])
@login_required
def check_in(activity_id):
    activity = visible_activity_or_404(activity_id)

    enrollment = db.session.execute(
        db.select(Enrollment).where(
            Enrollment.activity_id == activity.id,
            Enrollment.user_id == current_user.id,
        )
    ).scalar_one_or_none()

    if request.method == "POST":
        ok, message = check_in_by_code(
            activity.id, current_user.id, request.form.get("code")
        )
        if ok:
            flash("签到成功，祝你活动愉快。", "success")
            return redirect(
                url_for("activities.activity_detail", activity_id=activity.id)
            )
        flash(message, "warning")

    return render_template(
        "activities/check_in.html",
        activity=activity,
        enrollment=enrollment,
        check_in_state=check_in_state(activity, enrollment),
        state_labels=CHECK_IN_STATE_LABELS,
        state_styles=CHECK_IN_STATE_STYLES,
    )


# ---------------------------------------------------------------------------
# 报名名单 / 审核报名 / 核验签到 / 统计 / 导出（仅组织教师本人）
# ---------------------------------------------------------------------------


@bp.get("/activities/<int:activity_id>/enrollments")
@owner_required(lambda activity_id: get_activity_or_404(activity_id).organizer_id)
def activity_roster(activity_id):
    """报名名单：**待审核 / 正式 / 候补三段分列**（FR-7.4）。

    两条签到通道并存：组织者在正式名单上手动勾选（小活动最快），
    学生用 6 位签到码自助签到（几百人也不排队）。
    """
    # 页面上的活动切换器在没有脚本时靠表单 GET 提交，这里把它转成一次跳转，
    # 让地址栏始终是「某一场活动」的干净地址——刷新和分享都指向同一场活动。
    switched = request.args.get("__switch", type=int)
    if switched and switched != activity_id:
        return redirect(url_for("activities.activity_roster", activity_id=switched))

    activity = get_activity_or_404(activity_id)

    pending = (
        db.session.execute(
            db.select(Enrollment)
            .where(
                Enrollment.activity_id == activity.id,
                Enrollment.status == EnrollStatus.PENDING,
            )
            .order_by(Enrollment.enrolled_at)
        )
        .scalars()
        .all()
    )

    confirmed = (
        db.session.execute(
            db.select(Enrollment)
            .where(
                Enrollment.activity_id == activity.id,
                Enrollment.status == EnrollStatus.CONFIRMED,
            )
            .order_by(Enrollment.enrolled_at)
        )
        .scalars()
        .all()
    )

    # 候补按队列顺序展示（进入候补的时间，先到先得）
    waitlisted = (
        db.session.execute(waitlist_query(activity.id)).scalars().all()
    )

    # 页面上的活动切换器：让组织者不必退回列表再点进来
    siblings = (
        db.session.execute(
            db.select(Activity)
            .where(
                Activity.organizer_id == activity.organizer_id,
                Activity.status.in_(ActivityStatus.VISIBLE_TO_STUDENT),
            )
            .order_by(Activity.start_time.desc())
        )
        .scalars()
        .all()
    )

    return render_template(
        "activities/roster.html",
        activity=activity,
        pending=pending,
        confirmed=confirmed,
        waitlisted=waitlisted,
        stats=activity_stats(activity),
        siblings=siblings,
    )


@bp.post("/activities/<int:activity_id>/enrollments/<int:user_id>/approve")
@owner_required(lambda activity_id: get_activity_or_404(activity_id).organizer_id)
def approve_enrollment(activity_id, user_id):
    """审核通过一条报名申请。

    通过 ≠ 正式：学生能否立刻占上名额取决于**审批那一刻**的余位，
    这个判断在业务层里用原子占位完成，视图层不重复实现。
    """
    get_activity_or_404(activity_id)
    ok, message = review_enrollment(activity_id, user_id, current_user.id, True)
    flash(message, "success" if ok else "warning")
    return redirect(url_for("activities.activity_roster", activity_id=activity_id))


@bp.post("/activities/<int:activity_id>/enrollments/<int:user_id>/reject")
@owner_required(lambda activity_id: get_activity_or_404(activity_id).organizer_id)
def reject_enrollment(activity_id, user_id):
    """拒绝一条报名申请。拒绝是终态：本次不能参加，也不进入候补。"""
    get_activity_or_404(activity_id)
    ok, message = review_enrollment(activity_id, user_id, current_user.id, False)
    flash(message, "info" if ok else "warning")
    return redirect(url_for("activities.activity_roster", activity_id=activity_id))


@bp.post("/activities/<int:activity_id>/enrollments/<int:user_id>/check-in")
@owner_required(lambda activity_id: get_activity_or_404(activity_id).organizer_id)
def toggle_check_in(activity_id, user_id):
    """勾选 / 撤销某条报名记录的签到。"""
    # 用 hidden 字段的取值决定是勾上还是撤销。
    # 判断"当前是什么状态、所以应该取反"看起来很聪明，但在两个标签页
    # 同时操作时会互相打架，反而是显式指定更稳。
    checked_in = request.form.get("checked_in") == "1"
    ok, message = set_check_in(activity_id, user_id, current_user.id, checked_in)
    if not ok:
        flash(message, "warning")
    return redirect(
        url_for("activities.activity_roster", activity_id=activity_id)
    )


@bp.post("/activities/<int:activity_id>/check-in/open")
@owner_required(lambda activity_id: get_activity_or_404(activity_id).organizer_id)
def open_check_in(activity_id):
    """开启签到：活动进入「进行中」，签到码生成。"""
    activity = get_activity_or_404(activity_id)
    ok, message = open_check_in_service(activity)
    flash(
        message or "已开启签到。报名与取消报名通道同时关闭。",
        "success" if ok else "warning",
    )
    return redirect(url_for("activities.activity_roster", activity_id=activity_id))


@bp.get("/activities/<int:activity_id>/stats")
@owner_required(lambda activity_id: get_activity_or_404(activity_id).organizer_id)
def activity_stats_page(activity_id):
    activity = get_activity_or_404(activity_id)
    return render_template(
        "activities/stats.html",
        activity=activity,
        stats=activity_stats(activity),
    )


def _csv_cell(value):
    """把一个值转成 CSV 单元格。

    含逗号、引号或换行的字段必须用双引号包起来，内部的引号写成两个——
    否则「地点里带个逗号」就能把整张表挤歪一列。
    """
    text = "" if value is None else str(value)
    if any(ch in text for ch in (",", '"', "\n", "\r")):
        return '"' + text.replace('"', '""') + '"'
    return text


@bp.get("/activities/<int:activity_id>/enrollments/export")
@owner_required(lambda activity_id: get_activity_or_404(activity_id).organizer_id)
def export_enrollments(activity_id):
    """导出报名名单（含待审核与候补，用「状态」列区分）为 CSV。

    ⚠️ **必须带 UTF-8 BOM**。不带 BOM 的 UTF-8 文件，Excel 会按本地编码
    （简体中文 Windows 上是 GBK）去读，中文全部变成乱码。BOM 就是那几个
    看起来"多余"的字节在起作用。
    """
    activity = get_activity_or_404(activity_id)

    enrollments = (
        db.session.execute(
            db.select(Enrollment)
            .where(
                Enrollment.activity_id == activity.id,
                Enrollment.status.in_(EnrollStatus.ACTIVE),
            )
        )
        .scalars()
        .all()
    )

    # 正式在前（要带去现场点名的就是这批人），然后待审核、最后候补；
    # 候补内部严格按队列顺序（进入候补时间）。
    priority = {
        EnrollStatus.CONFIRMED: 0,
        EnrollStatus.PENDING: 1,
        EnrollStatus.WAITLISTED: 2,
    }
    enrollments.sort(
        key=lambda e: (
            priority.get(e.status, 9),
            e.waitlisted_at if e.status == EnrollStatus.WAITLISTED else e.enrolled_at,
            e.id,
        )
    )

    rows = [["序号", "状态", "学号", "姓名", "报名时间", "签到状态", "签到时间"]]
    for index, enrollment in enumerate(enrollments, start=1):
        rows.append(
            [
                index,
                enrollment.status_label,
                enrollment.user.student_no,
                enrollment.user.name,
                utc_iso(enrollment.enrolled_at),
                "已签到" if enrollment.has_checked_in else "未签到",
                utc_iso(enrollment.check_in_at),
            ]
        )

    # \r\n：CSV 标准里的行结束符，Excel 对它的兼容性最好
    body = "\r\n".join(",".join(_csv_cell(cell) for cell in row) for row in rows)
    payload = ("\ufeff" + body).encode("utf-8")

    filename = f"activity-{activity.id}-enrollments.csv"
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.get("/teacher/enrollments")
@role_required(Role.TEACHER)
def teacher_enrollments():
    """「报名与签到」入口：自动跳到最需要处理的那场活动。"""
    return _redirect_to_activity("activities.activity_roster")


@bp.get("/teacher/stats")
@role_required(Role.TEACHER)
def teacher_stats():
    """「数据统计」入口：全部活动的汇总 + 分活动明细。"""
    activities = (
        db.session.execute(
            db.select(Activity)
            .where(Activity.organizer_id == current_user.id)
            .order_by(Activity.start_time.desc())
        )
        .scalars()
        .all()
    )

    return render_template(
        "activities/organizer_stats.html",
        activities=activities,
        overview=organizer_overview([a.id for a in activities]),
        activity_stats=activity_stats,
    )


def _redirect_to_activity(endpoint):
    """挑一场最相关的活动跳过去：进行中 > 已发布 > 已结束 > 其它。

    组织者最常要做的是「正在进行的活动核验签到」，其次才是看下一场的报名情况。
    有「待审核申请」的已发布活动排在前面——有人等着处理，是最该先看到的。
    """
    priority = {
        ActivityStatus.ONGOING: 0,
        ActivityStatus.PUBLISHED: 1,
        ActivityStatus.FINISHED: 2,
        ActivityStatus.DRAFT: 3,
        ActivityStatus.OFFLINE: 4,
        ActivityStatus.CANCELED: 5,
    }
    activities = (
        db.session.execute(
            db.select(Activity).where(Activity.organizer_id == current_user.id)
        )
        .scalars()
        .all()
    )
    if not activities:
        return render_template(
            "activities/no_activity.html", endpoint=endpoint
        )

    def sort_key(activity):
        pending = pending_count(activity.id)
        return (priority.get(activity.status, 9), -pending, activity.start_time)

    activities.sort(key=sort_key)
    return redirect(url_for(endpoint, activity_id=activities[0].id))
