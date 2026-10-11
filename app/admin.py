"""管理员后台（V2.0）。

四件事——注意边界：**管理员不介入具体报名、不代替教师编辑活动内容**
（需求分析 3.1、A-07）。他能做的是平台级的状态处理：

* 账号管理 —— 创建账号、调整角色、停用 / 恢复账号；
* 活动监督 —— 查看平台全部活动，对违规或明显错误的活动下线 / 恢复展示；
* 分类管理 —— 增删改，删除前校验是否仍被活动引用；
* 密码找回 —— 处理「忘记密码」申请。

V1.0 的「审核中心（审核教师的发布申请）」已经移除：V2.0 教师直接发布，
审核发生在报名环节、由组织教师本人完成。
"""

import secrets

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user

from .config import MIN_PASSWORD_LENGTH
from .extensions import db
from .models import (
    Activity,
    ActivityStatus,
    Category,
    PasswordResetRequest,
    ResetRequestStatus,
    Role,
    User,
    utcnow,
)
from .permissions import role_required
from .services import (
    latest_offline_records,
    offline_activity as offline_service,
    restore_activity as restore_service,
)

bp = Blueprint("admin", __name__, url_prefix="/admin")

# 临时密码的字符集：刻意去掉 0 / O / 1 / l / I 这类容易看错的字符。
# 临时密码多半是口头念、手抄或者短信发的，看错一个字符就要重来一遍。
TEMP_PASSWORD_ALPHABET = (
    "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
)
TEMP_PASSWORD_LENGTH = 12


def generate_temp_password():
    """用密码学安全的随机源生成临时密码。

    不能用 random 模块——它基于可预测的伪随机序列，
    知道几个输出就能推出后续输出，等于没有随机性。
    """
    return "".join(
        secrets.choice(TEMP_PASSWORD_ALPHABET) for _ in range(TEMP_PASSWORD_LENGTH)
    )


@bp.get("/")
@role_required(Role.ADMIN)
def home():
    """管理后台首页。"""
    pending_resets = db.session.execute(
        db.select(db.func.count())
        .select_from(PasswordResetRequest)
        .where(PasswordResetRequest.status == ResetRequestStatus.PENDING)
    ).scalar_one()

    offline_activities = db.session.execute(
        db.select(db.func.count())
        .select_from(Activity)
        .where(Activity.status == ActivityStatus.OFFLINE)
    ).scalar_one()

    user_count = db.session.execute(
        db.select(db.func.count()).select_from(User)
    ).scalar_one()

    return render_template(
        "admin/home.html",
        pending_resets=pending_resets,
        offline_activities=offline_activities,
        user_count=user_count,
    )


# ---------------------------------------------------------------------------
# 活动监督（下线 / 恢复）
# ---------------------------------------------------------------------------


@bp.get("/activities")
@role_required(Role.ADMIN)
def all_activities():
    """平台全部活动，可按状态筛选。

    这是管理员在活动上的**唯一入口**：查看 + 状态处理（下线 / 恢复）。
    点进详情页只能看信息，不能替教师改内容、不能动报名记录。
    """
    status = request.args.get("status") or ""
    page = request.args.get("page", type=int) or 1
    keyword = (request.args.get("keyword") or "").strip()

    condition = db.select(Activity)
    if status in ActivityStatus.ALL:
        condition = condition.where(Activity.status == status)
    else:
        status = ""
    if keyword:
        condition = condition.where(Activity.title.like(f"%{keyword}%"))

    pagination = db.paginate(
        condition.order_by(Activity.start_time.desc()),
        page=page,
        per_page=15,
        error_out=False,
    )

    activities = pagination.items

    return render_template(
        "admin/activities.html",
        pagination=pagination,
        activities=activities,
        current_status=status,
        keyword=keyword,
        status_labels=ActivityStatus.LABELS,
        offinable_statuses=ActivityStatus.OFFINABLE,
        # 被下线的活动要显示「处理标记」：谁、什么时候、为什么（TC-14）
        offline_records=latest_offline_records([a.id for a in activities]),
    )


@bp.post("/activities/<int:activity_id>/offline")
@role_required(Role.ADMIN)
def offline_activity(activity_id):
    """下线违规或明显错误的活动。

    只改展示状态，**不删数据、不改内容、不动报名记录**（DEC-5）：
    学生端不再展示、不能报名；教师端仍可见并带着处理标记。
    """
    activity = db.session.get(Activity, activity_id)
    if activity is None:
        abort(404)

    ok, message = offline_service(
        activity, current_user.id, request.form.get("reason")
    )
    flash(message or "活动已下线，学生端不再展示。", "info" if ok else "warning")
    return redirect(request.form.get("next") or url_for("admin.all_activities"))


@bp.post("/activities/<int:activity_id>/restore")
@role_required(Role.ADMIN)
def restore_activity(activity_id):
    """恢复被下线的活动（按时间重新定位到已发布 / 进行中 / 已结束）。"""
    activity = db.session.get(Activity, activity_id)
    if activity is None:
        abort(404)

    ok, message = restore_service(
        activity, current_user.id, request.form.get("reason")
    )
    flash(message or "活动已恢复展示。", "success" if ok else "warning")
    return redirect(request.form.get("next") or url_for("admin.all_activities"))


# ---------------------------------------------------------------------------
# 分类管理
# ---------------------------------------------------------------------------


@bp.get("/categories")
@role_required(Role.ADMIN)
def categories():
    """分类维护。顺带统计每个分类下有多少活动，删之前能一眼看出影响面。"""
    rows = db.session.execute(
        db.select(Category, db.func.count(Activity.id))
        .outerjoin(Activity, Activity.category_id == Category.id)
        .group_by(Category.id)
        .order_by(Category.sort_order, Category.id)
    ).all()

    return render_template(
        "admin/categories.html",
        rows=rows,
    )


@bp.post("/categories")
@role_required(Role.ADMIN)
def create_category():
    name = (request.form.get("name") or "").strip()
    sort_order = request.form.get("sort_order", type=int) or 0

    if not name:
        flash("分类名称不能为空。", "danger")
        return redirect(url_for("admin.categories"))
    if len(name) > 30:
        flash("分类名称不能超过 30 字。", "danger")
        return redirect(url_for("admin.categories"))

    existing = db.session.execute(
        db.select(Category).where(Category.name == name)
    ).scalar_one_or_none()
    if existing is not None:
        flash(f"分类「{name}」已经存在了。", "warning")
        return redirect(url_for("admin.categories"))

    db.session.add(Category(name=name, sort_order=sort_order))
    db.session.commit()
    flash(f"分类「{name}」已创建。", "success")
    return redirect(url_for("admin.categories"))


@bp.post("/categories/<int:category_id>")
@role_required(Role.ADMIN)
def update_category(category_id):
    category = db.session.get(Category, category_id)
    if category is None:
        abort(404)

    name = (request.form.get("name") or "").strip()
    if not name or len(name) > 30:
        flash("分类名称不能为空且不超过 30 字。", "danger")
        return redirect(url_for("admin.categories"))

    duplicate = db.session.execute(
        db.select(Category).where(Category.name == name, Category.id != category_id)
    ).scalar_one_or_none()
    if duplicate is not None:
        flash(f"分类「{name}」已经存在了。", "warning")
        return redirect(url_for("admin.categories"))

    category.name = name
    category.sort_order = request.form.get("sort_order", type=int) or 0
    db.session.commit()
    flash("分类已更新。", "success")
    return redirect(url_for("admin.categories"))


@bp.post("/categories/<int:category_id>/delete")
@role_required(Role.ADMIN)
def delete_category(category_id):
    """删除分类。

    删除前必须校验是否仍被活动引用——直接删会让那些活动的 `category_id`
    指向一个不存在的分类，列表页上就是一串空白，而且很难查是哪里出的问题。
    """
    category = db.session.get(Category, category_id)
    if category is None:
        abort(404)

    used = db.session.execute(
        db.select(db.func.count())
        .select_from(Activity)
        .where(Activity.category_id == category_id)
    ).scalar_one()
    if used:
        flash(f"「{category.name}」下还有 {used} 场活动，不能删除。请先调整这些活动的分类。", "danger")
        return redirect(url_for("admin.categories"))

    db.session.delete(category)
    db.session.commit()
    flash(f"分类「{category.name}」已删除。", "info")
    return redirect(url_for("admin.categories"))


# ---------------------------------------------------------------------------
# 用户与角色
# ---------------------------------------------------------------------------


@bp.get("/users")
@role_required(Role.ADMIN)
def users():
    """用户列表。系统不提供自助注册，账号一律由管理员在这里创建。"""
    keyword = (request.args.get("keyword") or "").strip()

    condition = db.select(User)
    if keyword:
        pattern = f"%{keyword}%"
        condition = condition.where(
            db.or_(User.student_no.like(pattern), User.name.like(pattern))
        )

    people = (
        db.session.execute(condition.order_by(User.role, User.student_no))
        .scalars()
        .all()
    )

    return render_template(
        "admin/users.html",
        users=people,
        keyword=keyword,
        role_labels=Role.LABELS,
        min_password_length=MIN_PASSWORD_LENGTH,
    )


@bp.post("/users")
@role_required(Role.ADMIN)
def create_user():
    student_no = (request.form.get("student_no") or "").strip()
    name = (request.form.get("name") or "").strip()
    password = request.form.get("password") or ""
    role = (request.form.get("role") or Role.STUDENT).upper()

    errors = []
    if not student_no or len(student_no) > 20:
        errors.append("学号 / 工号不能为空且不超过 20 字")
    if not name or len(name) > 50:
        errors.append("姓名不能为空且不超过 50 字")
    if role not in Role.ALL:
        errors.append("角色取值不合法")
    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(f"初始密码不能少于 {MIN_PASSWORD_LENGTH} 个字符")

    if not errors:
        exists = db.session.execute(
            db.select(User).where(User.student_no == student_no)
        ).scalar_one_or_none()
        if exists is not None:
            errors.append(f"学号 / 工号 {student_no} 已经存在")

    if errors:
        for message in errors:
            flash(message, "danger")
        return redirect(url_for("admin.users"))

    user = User(
        student_no=student_no,
        name=name,
        role=role,
        email=(request.form.get("email") or "").strip() or None,
        club_name=(request.form.get("club_name") or "").strip() or None,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    flash(f"已创建账号：{user.student_no} {user.name}（{user.role_label}）", "success")
    return redirect(url_for("admin.users"))


def _admin_count():
    return db.session.execute(
        db.select(db.func.count()).select_from(User).where(User.role == Role.ADMIN)
    ).scalar_one()


@bp.post("/users/<int:user_id>")
@role_required(Role.ADMIN)
def update_user(user_id):
    """调整角色 / 停用账号。

    两条防止把自己锁在门外的护栏：

    * 不能停用或降级**自己的**账号；
    * 不能把**最后一个**管理员降级或停用。

    这两条都不是需求文档要求的，但没有它们，管理员点错一下系统就没人能管了，
    而且是不可逆的——重装数据库才能救回来。
    """
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)

    role = (request.form.get("role") or user.role).upper()
    status = request.form.get("status", type=int)
    if status not in (0, 1):
        status = user.status

    if role not in Role.ALL:
        flash("角色取值不合法。", "danger")
        return redirect(url_for("admin.users"))

    if user.id == current_user.id:
        if role != Role.ADMIN or status != 1:
            flash("不能降级或停用自己的账号，否则你会立刻失去管理权限。", "danger")
            return redirect(url_for("admin.users"))
    elif user.role == Role.ADMIN and (role != Role.ADMIN or status == 0):
        if _admin_count() <= 1:
            flash("这是系统中最后一个管理员，不能降级或停用。", "danger")
            return redirect(url_for("admin.users"))

    user.role = role
    user.status = status
    user.club_name = (request.form.get("club_name") or "").strip() or None
    db.session.commit()

    flash(f"已更新 {user.name} 的角色与状态。", "success")
    return redirect(url_for("admin.users"))


@bp.post("/users/<int:user_id>/reset-password")
@role_required(Role.ADMIN)
def reset_user_password(user_id):
    """直接把某个账号重置为随机临时密码（用于管理员当场核实身份的场景）。"""
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)

    temp_password = generate_temp_password()
    user.set_password(temp_password)
    user.must_change_password = True
    user.reset_login_failures()
    db.session.commit()

    # 临时密码只在这一次响应里显示，同样不走「重定向后显示」——
    # 那等于把凭证写进客户端 Cookie
    return render_template(
        "admin/password_reset_done.html",
        target=user,
        temp_password=temp_password,
        min_length=MIN_PASSWORD_LENGTH,
    )


@bp.get("/password-resets")
@role_required(Role.ADMIN)
def password_resets():
    """找回申请：待处理队列 + 最近处理记录。"""
    pending = (
        db.session.execute(
            db.select(PasswordResetRequest)
            .where(PasswordResetRequest.status == ResetRequestStatus.PENDING)
            .order_by(PasswordResetRequest.created_at)
        )
        .scalars()
        .all()
    )

    handled = (
        db.session.execute(
            db.select(PasswordResetRequest)
            .where(PasswordResetRequest.status != ResetRequestStatus.PENDING)
            .order_by(PasswordResetRequest.handled_at.desc())
            .limit(20)
        )
        .scalars()
        .all()
    )

    return render_template(
        "admin/password_resets.html", pending=pending, handled=handled
    )


@bp.post("/password-resets/<int:request_id>/resolve")
@role_required(Role.ADMIN)
def resolve_password_reset(request_id):
    """核实身份后，把密码重置为随机临时密码。

    **临时密码只在这里显示一次**，所以刻意不走「重定向后显示」那套：
    临时密码是要交给本人的凭证，塞进 session（签名但不加密的 Cookie）等于
    把它写到了客户端。直接在 POST 的响应里渲染，用过即散，两端都不留存。

    为什么不让管理员自己指定新密码：那样管理员就知道了用户的密码，账号失去
    「只有本人知道密码」这个基本属性。系统只发临时密码，并强制本人登录后立刻改掉。
    """
    reset = db.session.get(PasswordResetRequest, request_id)
    if reset is None:
        abort(404)
    if reset.status != ResetRequestStatus.PENDING:
        flash("这条申请已经处理过了。", "warning")
        return redirect(url_for("admin.password_resets"))

    target = db.session.get(User, reset.user_id)
    if target is None:
        abort(404)

    temp_password = generate_temp_password()
    target.set_password(temp_password)
    target.must_change_password = True
    # 管理员既然已经核实了身份，之前因反复登录失败而锁定状态也一并解除
    target.reset_login_failures()

    reset.status = ResetRequestStatus.RESOLVED
    reset.handled_by = current_user.id
    reset.handled_at = utcnow()
    reset.handle_note = "已下发临时密码"
    db.session.commit()

    return render_template(
        "admin/password_reset_done.html",
        target=target,
        temp_password=temp_password,
        min_length=MIN_PASSWORD_LENGTH,
    )


@bp.post("/password-resets/<int:request_id>/reject")
@role_required(Role.ADMIN)
def reject_password_reset(request_id):
    """核实身份不通过时，把申请标记为未通过。"""
    reset = db.session.get(PasswordResetRequest, request_id)
    if reset is None:
        abort(404)

    if reset.status == ResetRequestStatus.PENDING:
        reset.status = ResetRequestStatus.REJECTED
        reset.handled_by = current_user.id
        reset.handled_at = utcnow()
        reset.handle_note = (request.form.get("note") or "").strip()[:255] or None
        db.session.commit()
        flash("已标记为未通过。", "info")

    return redirect(url_for("admin.password_resets"))
