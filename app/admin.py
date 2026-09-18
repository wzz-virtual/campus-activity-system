"""管理员后台。

M0 阶段这里只有一件事：处理「忘记密码」的找回申请。
待审核活动队列在 M2 补上。
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
    PasswordResetRequest,
    ResetRequestStatus,
    Role,
    User,
    utcnow,
)
from .permissions import role_required

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

    return render_template("admin/home.html", pending_resets=pending_resets)


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
