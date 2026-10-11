"""认证：登录、登出、当前用户。

用 Flask 内置的签名 session（Cookie）保存登录态，比 JWT 简单且完全够用。
等将来真正做前后端分离时再换成 token。

这个模块是整个系统最需要小心的地方：一旦这里被绕过，后面所有的角色校验、
归属权校验都形同虚设。所以下面每一处防护都写了「为什么」。
"""

from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from .config import LOGIN_LOCK_DURATION, LOGIN_MAX_ATTEMPTS, MIN_PASSWORD_LENGTH
from .extensions import db
from .models import PasswordResetRequest, ResetRequestStatus, Role, User

bp = Blueprint("auth", __name__, url_prefix="/auth")

# 登录失败的提示语。账号不存在和密码错误必须用**同一句**，
# 否则攻击者能靠提示语的差异判断出哪些学号是真实存在的。
LOGIN_FAILED_MESSAGE = "学号或密码不正确"

# 一个固定的、谁也不知道原文的密码哈希。
# 用途：当账号不存在时，仍然拿它跑一遍哈希计算。
# 不这么做的话，「查不到用户 → 立刻返回」和「查到用户 → 算一次哈希再返回」
# 之间会有几十到上百毫秒的差距，攻击者逐条计时就能把有效学号筛出来。
_DUMMY_PASSWORD_HASH = generate_password_hash("timing-equalisation-dummy-value")


def _is_safe_local_path(url):
    """判断 next 参数是不是一个可以安全跳转的站内路径。

    登录成功后按 ?next= 跳转是常见需求，但如果不校验，攻击者可以构造
    ``/auth/login?next=https://evil.com``，让用户登录后被送到钓鱼站。
    这就是「开放重定向」。

    这里只放行站内路径，几种典型的绕过形态都挡掉：

    * ``https://evil.com``、``javascript:alert(1)`` —— 不以 "/" 开头
    * ``//evil.com`` —— 协议相对地址，浏览器会当成 https://evil.com
    * ``/\\evil.com`` —— 浏览器把反斜杠视同正斜杠，等于上面那条
    * 混入 \\t \\n \\r —— 有些解析器会先把控制字符去掉再做前缀判断
    """
    if not url or not url.startswith("/"):
        return False
    if url.startswith("//"):
        return False
    if "\\" in url:
        return False
    if any(ch in url for ch in "\t\n\r\x00"):
        return False
    return True


def _safe_next(default_endpoint):
    """取 ?next= / 表单里的 next，不安全就退回默认页面。"""
    next_url = request.args.get("next") or request.form.get("next")
    if _is_safe_local_path(next_url):
        return next_url
    return url_for(default_endpoint)


def _find_user(student_no):
    return db.session.execute(
        db.select(User).where(User.student_no == student_no)
    ).scalar_one_or_none()


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        student_no = (request.form.get("student_no") or "").strip()
        password = request.form.get("password") or ""

        user = _find_user(student_no)

        # 账号不存在：照跑一次哈希运算再返回同样的提示。
        if user is None:
            check_password_hash(_DUMMY_PASSWORD_HASH, password)
            flash(LOGIN_FAILED_MESSAGE, "danger")
            return render_template("auth/login.html"), 401

        # 锁定期内直接拒绝，连哈希都不用算
        if user.is_locked():
            remaining = user.lock_remaining_seconds()
            minutes = max(1, remaining // 60 + 1)
            flash(
                f"该账号因连续多次登录失败已被临时锁定，请约 {minutes} 分钟后再试。",
                "warning",
            )
            return render_template("auth/login.html"), 429

        if not user.check_password(password):
            user.register_failed_login(LOGIN_MAX_ATTEMPTS, LOGIN_LOCK_DURATION)
            db.session.commit()
            flash(LOGIN_FAILED_MESSAGE, "danger")
            return render_template("auth/login.html"), 401

        # 密码对了才检查账号状态，避免在没通过验证的情况下泄漏账号是否被停用
        if not user.is_active:
            flash("该账号已被停用，请联系管理员", "danger")
            return render_template("auth/login.html"), 403

        # 防会话固定（session fixation）：
        # 攻击者如果能事先把自己的 session Cookie 塞进受害者浏览器，
        # 登录后双方就共用了同一个会话。登录时清空并重建会话可以断掉这条路。
        # 注意：必须在 login_user() 之前清，否则会把刚写入的登录态一起清掉。
        session.clear()
        # 标记为永久会话，好让 SESSION_LIFETIME（8 小时）真正生效；
        # 不设置的话浏览器一关就失效，服务端也没有过期时间。
        session.permanent = True

        user.reset_login_failures()
        db.session.commit()

        login_user(user)

        # 密码是被管理员重置过的，先逼本人改掉再放行
        if user.must_change_password:
            flash("当前使用的是管理员下发的临时密码，请先设置新密码。", "warning")
            return redirect(url_for("auth.change_password"))

        flash(f"欢迎回来，{user.name}", "success")
        return redirect(_safe_next("main.dashboard"))

    return render_template("auth/login.html")


@bp.route("/register", methods=["GET", "POST"])
def register():
    """学生自助注册。

    规则（按需求明确）：

    * **同一个学号 / 工号只能注册一个账号**——重复注册会被拒绝，
      而且**绝不会覆盖已有账号的密码**（密码以首次注册时输入的为准）；
    * 角色**写死为学生**：教师 / 管理员账号只能由管理员在后台创建。
      请求体里塞 role=ADMIN 不会有任何效果——角色的唯一来源是服务端。
    """
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        student_no = (request.form.get("student_no") or "").strip()
        name = (request.form.get("name") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""

        error = None
        if not student_no or len(student_no) > 20:
            error = "学号 / 工号不能为空，且不超过 20 个字"
        elif not name or len(name) > 50:
            error = "姓名不能为空，且不超过 50 个字"
        elif len(password) < MIN_PASSWORD_LENGTH:
            error = f"密码长度不能少于 {MIN_PASSWORD_LENGTH} 个字符"
        elif password != confirm:
            error = "两次输入的密码不一致"
        elif _find_user(student_no) is not None:
            # 学号已被注册过：拒绝，并且**不动**已有账号的任何东西
            error = "这个学号 / 工号已经注册过了。如果是你本人，请直接登录；忘记密码可以走「找回密码」。"

        if error:
            flash(error, "danger")
            return (
                render_template(
                    "auth/register.html",
                    form={"student_no": student_no, "name": name},
                    min_password_length=MIN_PASSWORD_LENGTH,
                ),
                400,
            )

        user = User(student_no=student_no, name=name, role=Role.STUDENT)
        user.set_password(password)
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            # 并发兜底：两个人同时用同一个学号注册，唯一索引会拦下后来的那个
            db.session.rollback()
            flash("这个学号 / 工号刚刚被注册过了，请直接登录。", "warning")
            return (
                render_template(
                    "auth/register.html",
                    form={"student_no": student_no, "name": name},
                    min_password_length=MIN_PASSWORD_LENGTH,
                ),
                409,
            )

        # 注册成功直接进入登录态；会话防护与登录保持一致：
        # 先清 session（防会话固定），再标记永久会话（让 8 小时有效期生效）。
        session.clear()
        session.permanent = True
        login_user(user)

        flash(f"注册成功，欢迎你，{user.name}", "success")
        return redirect(url_for("main.dashboard"))

    return render_template(
        "auth/register.html",
        form={"student_no": "", "name": ""},
        min_password_length=MIN_PASSWORD_LENGTH,
    )


@bp.post("/logout")
@login_required
def logout():
    logout_user()
    # 登出同样清空会话：既销毁登录态，也顺带作废 session 里的 CSRF token 等残留
    session.clear()
    flash("已退出登录", "info")
    return redirect(url_for("main.index"))


@bp.get("/me")
@login_required
def me():
    """返回当前登录用户的信息，供前端脚本使用。

    注意这里返回的 role 来自数据库，不是前端传来的任何东西——
    角色永远只能由服务端决定。
    """
    return jsonify(current_user.to_dict())


def _has_pending_reset_request(user_id):
    return (
        db.session.execute(
            db.select(PasswordResetRequest.id).where(
                PasswordResetRequest.user_id == user_id,
                PasswordResetRequest.status == ResetRequestStatus.PENDING,
            )
        ).first()
        is not None
    )


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """忘记密码：提交找回申请，由管理员核实身份后重置。

    ⚠️ 这个页面同样不能泄漏「哪些学号是真实存在的」——无论学号存不存在，
    都返回**同一句**提示。否则它就成了一个比登录页更好用的账号枚举工具：
    登录页还有失败次数限制，这里没有。

    同一个用户只保留一条待处理申请，避免有人反复提交把管理员的队列刷满。
    """
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        student_no = (request.form.get("student_no") or "").strip()
        note = (request.form.get("note") or "").strip()

        user = _find_user(student_no)
        if user is not None and not _has_pending_reset_request(user.id):
            db.session.add(
                PasswordResetRequest(user_id=user.id, note=note[:255] or None)
            )
            db.session.commit()

        flash("已收到申请。管理员核实身份后会为你重置密码，请留意联系方式。", "info")
        return redirect(url_for("auth.login"))

    return render_template("auth/forgot_password.html")


@bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    """本人修改密码。

    要求先验证当前密码：即使有人趁你离开工位用你已登录的浏览器，
    也改不掉密码。
    """
    if request.method == "POST":
        current_password = request.form.get("current_password") or ""
        new_password = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        error = None
        if not current_user.check_password(current_password):
            error = "当前密码不正确"
        elif len(new_password) < MIN_PASSWORD_LENGTH:
            error = f"新密码长度不能少于 {MIN_PASSWORD_LENGTH} 个字符"
        elif new_password != confirm_password:
            error = "两次输入的新密码不一致"
        elif new_password == current_password:
            error = "新密码不能与当前密码相同"

        if error:
            flash(error, "danger")
            return render_template("auth/change_password.html"), 400

        current_user.set_password(new_password)
        # 临时密码用完即作废，解除强制改密
        current_user.must_change_password = False
        db.session.commit()

        flash("密码已更新", "success")
        return redirect(url_for("main.dashboard"))

    return render_template("auth/change_password.html")
