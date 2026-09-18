"""主页面路由。

M0 阶段只提供骨架页面：首页、工作台，以及两个用来验证角色权限的落点页面。
活动列表、详情、报名等页面在 M1 / M3 补上。
"""

from flask import Blueprint, render_template
from flask_login import current_user, login_required

from .models import Role
from .permissions import role_required

bp = Blueprint("main", __name__)


@bp.get("/")
def index():
    """首页。未登录也能看，但只展示公开内容。"""
    return render_template("main/index.html")


@bp.get("/dashboard")
@login_required
def dashboard():
    """工作台。三种角色看到的内容不一样。"""
    return render_template("main/dashboard.html")


@bp.get("/club/")
@role_required(Role.CLUB)
def club_home():
    """社团工作台。我的活动列表在 M1 补上。"""
    return render_template("main/club.html")


@bp.get("/health")
def health():
    """健康检查，给部署脚本和冒烟测试用。"""
    return {"status": "ok", "authenticated": current_user.is_authenticated}
