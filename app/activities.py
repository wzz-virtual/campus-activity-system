"""活动：创建、编辑、列表、详情、审核流转，以及报名与签到的入口。

路由分两类：

* **页面路由**（GET）—— 返回渲染好的 HTML；
* **动作路由**（POST）—— 接收表单，处理完**重定向回页面**，即 Post/Redirect/Get。
  直接返回渲染结果会导致用户一刷新就重复提交一次（取消报名这种操作重复执行
  的后果是实打实的）。重定向之后，浏览器地址栏是一个普通 GET 地址，
  刷新只会重新拉一次页面。

权限的做法统一为：装饰器拦第一道（角色 / 归属权），视图里再按**状态**拦第二道。
前端把按钮藏起来不算权限控制——改一个请求就能绕过。
"""

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
from .models import (
    Activity,
    ActivityStatus,
    AuditAction,
    AuditLog,
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
    activity_stats,
    approve_activity as approve_activity_service,
    cancel_activity as cancel_activity_service,
    cancel_blocked_reason,
    cancel_deadline_text,
    cancel_enrollment,
    can_edit_activity,
    check_in_by_code,
    check_in_state,
    collect_activity_form,
    edit_blocked_reason,
    enroll,
    enroll_blocked_reason,
    open_check_in as open_check_in_service,
    organizer_overview,
    reject_activity as reject_activity_service,
    set_check_in,
    submit_for_review,
    validate_activity_form,
    withdraw_to_draft,
)
from .timeutils import format_local_input, utc_iso

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
    草稿、待审核、已驳回对创建者和管理员以外的人**完全不可见**——
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

    * 只展示对学生可见的状态；
    * 支持分类 / 状态 / 关键词筛选；
    * **必须分页**，不允许一次查出全表。
    """
    page = request.args.get("page", type=int) or 1
    category_id = request.args.get("category", type=int)
    status = request.args.get("status") or ""
    keyword = (request.args.get("keyword") or "").strip()

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

    # 默认按开始时间倒序：最新的活动排在前面
    pagination = db.paginate(
        condition.order_by(Activity.start_time.desc()),
        page=page,
        per_page=PAGE_SIZE,
        error_out=False,
    )

    now = utcnow()
    return render_template(
        "activities/list.html",
        pagination=pagination,
        activities=pagination.items,
        categories=_categories(),
        my_enrollments=_my_enrollment_map([a.id for a in pagination.items]),
        current_category=category_id,
        current_status=status,
        keyword=keyword,
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
    }


@bp.get("/activities/new")
@role_required(Role.CLUB, Role.ADMIN)
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
    ):
        setattr(activity, name, parsed[name])


@bp.post("/activities")
@role_required(Role.CLUB, Role.ADMIN)
def create_activity():
    """创建活动。

    两种提交方式：

    * ``action=draft`` —— 存草稿，先不惊动管理员；
    * ``action=submit`` —— 社团提交审核（进 `PENDING`），管理员直接发布（进 `PUBLISHED`）。

    管理员创建的活动**跳过审核**，这个判断只看 ``current_user.role``，
    而角色只能来自数据库——登录表单里多传一个 ``role=ADMIN`` 不会有任何效果。
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

    if wants_draft and not current_user.is_club:
        flash("只有社团账号需要存草稿，管理员创建的活动直接发布。", "warning")
        return redirect(url_for("activities.new_activity"))

    activity = Activity(organizer_id=current_user.id, enrolled_count=0)
    if wants_draft:
        # 草稿允许字段不完整，所以用「缺什么补什么」的默认值兜底，
        # 强制校验留到提交审核那一步再做。
        _apply_form(activity, _draft_defaults(parsed))
        activity.status = ActivityStatus.DRAFT
        flash("已存为草稿，可以稍后继续编辑或提交审核。", "success")
    else:
        _apply_form(activity, parsed)
        if current_user.is_admin:
            activity.status = ActivityStatus.PUBLISHED
            activity.approved_by = current_user.id
            activity.approved_at = utcnow()
            flash("活动已直接发布。", "success")
        else:
            activity.status = ActivityStatus.PENDING
            flash("活动已提交审核，管理员通过后就会出现在活动广场。", "success")

    db.session.add(activity)
    db.session.flush()
    if activity.status == ActivityStatus.PENDING:
        # 首次提交也是审核流的一环，同样要留痕
        db.session.add(
            AuditLog(
                activity_id=activity.id,
                operator_id=current_user.id,
                action=AuditAction.SUBMIT,
            )
        )
    db.session.commit()

    return redirect(url_for("activities.activity_detail", activity_id=activity.id))


def _draft_defaults(parsed):
    """草稿的兜底值。

    数据库里这些列都是 NOT NULL，但草稿本来就允许没填完，
    所以缺的字段先塞一个占位值，等真正提交审核时再要求填全。
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
    db.session.commit()
    flash("活动信息已更新。", "success")
    return redirect(url_for("activities.activity_detail", activity_id=activity.id))


@bp.post("/activities/<int:activity_id>/delete")
@owner_required(lambda activity_id: get_activity_or_404(activity_id).organizer_id)
def delete_activity(activity_id):
    """删除草稿。只有草稿能删——已发布的活动一旦有人报名，删掉就是毁数据。"""
    activity = get_activity_or_404(activity_id)
    if activity.status != ActivityStatus.DRAFT:
        flash("只有草稿可以删除。已提交过的活动请使用「取消活动」。", "warning")
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
    can_enroll = False
    blocked_reason = None
    if not current_user.is_authenticated:
        blocked_reason = "请先登录后再报名"
    elif enrollment is not None and enrollment.status == EnrollStatus.ENROLLED:
        blocked_reason = None
    else:
        # 这里只是**提前告知**按钮状态。真正的判断在 services.enroll() 里，
        # 提交时还会在数据库层再判一次——前端提示不能当作校验。
        blocked_reason = enroll_blocked_reason(activity.id, now)
        can_enroll = blocked_reason is None

    cancel_reason = None
    if enrollment is not None and enrollment.status == EnrollStatus.ENROLLED:
        cancel_reason = cancel_blocked_reason(activity, enrollment, now)

    return render_template(
        "activities/detail.html",
        activity=activity,
        enrollment=enrollment,
        can_enroll=can_enroll,
        blocked_reason=blocked_reason,
        cancel_reason=cancel_reason,
        cancel_until=cancel_deadline_text(activity)
        if cancel_reason is None
        else None,
        check_in_state=check_in_state(activity, enrollment, now),
        check_in_state_labels=CHECK_IN_STATE_LABELS,
        check_in_state_styles=CHECK_IN_STATE_STYLES,
        stats=activity_stats(activity)
        if current_user.is_authenticated
        and (current_user.is_admin or activity.organizer_id == current_user.id)
        else None,
        can_edit=current_user.is_authenticated and can_edit_activity(current_user, activity),
        can_manage=current_user.is_authenticated
        and (current_user.is_admin or activity.organizer_id == current_user.id),
        status_labels=ActivityStatus.LABELS,
        now=now,
    )


# ---------------------------------------------------------------------------
# 状态流转
# ---------------------------------------------------------------------------


@bp.post("/activities/<int:activity_id>/submit")
@owner_required(lambda activity_id: get_activity_or_404(activity_id).organizer_id)
def submit_activity(activity_id):
    activity = get_activity_or_404(activity_id)
    ok, message = submit_for_review(activity, current_user.id)
    flash(message or "已提交审核，请等待管理员处理。", "success" if ok else "warning")
    return redirect(url_for("activities.activity_detail", activity_id=activity_id))


@bp.post("/activities/<int:activity_id>/withdraw")
@owner_required(lambda activity_id: get_activity_or_404(activity_id).organizer_id)
def withdraw_activity(activity_id):
    activity = get_activity_or_404(activity_id)
    ok, message = withdraw_to_draft(activity)
    flash(message or "已撤回为草稿。", "info" if ok else "warning")
    return redirect(url_for("activities.activity_detail", activity_id=activity_id))


@bp.post("/activities/<int:activity_id>/approve")
@role_required(Role.ADMIN)
def approve_activity(activity_id):
    activity = get_activity_or_404(activity_id)
    ok, message = approve_activity_service(activity, current_user.id)
    flash(message or "已通过审核，活动已在活动广场发布。", "success" if ok else "warning")
    return redirect(request.form.get("next") or url_for("admin.audit_center"))


@bp.post("/activities/<int:activity_id>/reject")
@role_required(Role.ADMIN)
def reject_activity(activity_id):
    activity = get_activity_or_404(activity_id)
    ok, message = reject_activity_service(
        activity, current_user.id, request.form.get("reason")
    )
    flash(message or "已驳回，原因已记录并可被创建者看到。", "info" if ok else "danger")
    return redirect(request.form.get("next") or url_for("admin.audit_center"))


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
# 报名与取消报名
# ---------------------------------------------------------------------------


@bp.post("/activities/<int:activity_id>/enroll")
@login_required
def enroll_activity(activity_id):
    get_activity_or_404(activity_id)
    ok, message = enroll(activity_id, current_user.id)
    if ok:
        flash("报名成功，名额已为你保留。", "success")
    else:
        flash(message, "warning")
    return redirect(url_for("activities.activity_detail", activity_id=activity_id))


@bp.post("/activities/<int:activity_id>/unenroll")
@login_required
def unenroll_activity(activity_id):
    get_activity_or_404(activity_id)
    ok, message = cancel_enrollment(activity_id, current_user.id)
    if ok:
        flash("已取消报名，名额已释放。", "info")
    else:
        flash(message, "warning")
    return redirect(url_for("activities.activity_detail", activity_id=activity_id))


@bp.get("/my/enrollments")
@login_required
def my_enrollments():
    """我的报名。逐条给出签到状态，并把「还能不能取消」和原因写清楚。"""
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
        rows.append(
            {
                "enrollment": enrollment,
                "activity": activity,
                "state": check_in_state(activity, enrollment, now),
                "cancel_reason": cancel_blocked_reason(activity, enrollment, now)
                if enrollment.status == EnrollStatus.ENROLLED
                else None,
                # 还能取消时，明确写出可以反悔到什么时候
                "cancel_until": cancel_deadline_text(activity)
                if enrollment.status == EnrollStatus.ENROLLED
                else None,
            }
        )

    return render_template(
        "activities/my_enrollments.html",
        rows=rows,
        state_labels=CHECK_IN_STATE_LABELS,
        state_styles=CHECK_IN_STATE_STYLES,
        cancel_lock_hours=CANCEL_LOCK_HOURS,
    )


# ---------------------------------------------------------------------------
# 我的活动（创建者视角）
# ---------------------------------------------------------------------------


@bp.get("/my/activities")
@role_required(Role.CLUB, Role.ADMIN)
def my_activities():
    """我的活动。按状态分组，并按状态给出这一状态下能做的操作。"""
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
# 报名名单 / 核验签到 / 统计 / 导出（仅创建者与管理员）
# ---------------------------------------------------------------------------


@bp.get("/activities/<int:activity_id>/enrollments")
@owner_required(lambda activity_id: get_activity_or_404(activity_id).organizer_id)
def activity_roster(activity_id):
    """报名名单 + 签到核验。

    两条签到通道并存：组织者在名单上手动勾选（小活动最快），
    学生用 6 位签到码自助签到（几百人也不排队）。
    """
    # 页面上的活动切换器在没有脚本时靠表单 GET 提交，这里把它转成一次跳转，
    # 让地址栏始终是「某一场活动」的干净地址——刷新和分享都指向同一场活动。
    switched = request.args.get("__switch", type=int)
    if switched and switched != activity_id:
        return redirect(url_for("activities.activity_roster", activity_id=switched))

    activity = get_activity_or_404(activity_id)

    enrollments = (
        db.session.execute(
            db.select(Enrollment)
            .where(
                Enrollment.activity_id == activity.id,
                Enrollment.status == EnrollStatus.ENROLLED,
            )
            .order_by(Enrollment.enrolled_at)
        )
        .scalars()
        .all()
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
        enrollments=enrollments,
        stats=activity_stats(activity),
        siblings=siblings,
    )


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
    """导出报名名单为 CSV。

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
                Enrollment.status == EnrollStatus.ENROLLED,
            )
            .order_by(Enrollment.enrolled_at)
        )
        .scalars()
        .all()
    )

    rows = [["序号", "学号", "姓名", "报名时间", "签到状态", "签到时间"]]
    for index, enrollment in enumerate(enrollments, start=1):
        rows.append(
            [
                index,
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


@bp.get("/club/enrollments")
@role_required(Role.CLUB, Role.ADMIN)
def club_enrollments():
    """「报名与签到」入口：自动跳到最需要处理的那场活动。"""
    return _redirect_to_activity("activities.activity_roster")


@bp.get("/club/stats")
@role_required(Role.CLUB, Role.ADMIN)
def club_stats():
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
    """
    priority = {
        ActivityStatus.ONGOING: 0,
        ActivityStatus.PUBLISHED: 1,
        ActivityStatus.FINISHED: 2,
        ActivityStatus.PENDING: 3,
        ActivityStatus.DRAFT: 4,
        ActivityStatus.REJECTED: 5,
        ActivityStatus.CANCELED: 6,
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

    activities.sort(key=lambda a: (priority.get(a.status, 9), a.start_time))
    return redirect(url_for(endpoint, activity_id=activities[0].id))
