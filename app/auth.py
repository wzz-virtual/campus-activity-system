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
from werkzeug.security import check_password_hash, generate_password_hash

from .config import LOGIN_LOCK_DURATION, LOGIN_MAX_ATTEMPTS
from .extensions import db
from .models import User

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
        flash(f"欢迎回来，{user.name}", "success")
        return redirect(_safe_next("main.dashboard"))

    return render_template("auth/login.html")


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
