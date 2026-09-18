"""主页面路由：首页、工作台、社团工作台、健康检查。"""

from flask import Blueprint, render_template
from flask_login import current_user, login_required

from .extensions import db
from .models import Activity, ActivityStatus, Role, Enrollment, EnrollStatus, utcnow
from .permissions import role_required
from .services import activity_stats

bp = Blueprint("main", __name__)


@bp.get("/")
def index():
    """首页。未登录也能看，展示最近的活动但不涉及任何需要登录的信息。"""
    now = utcnow()

    upcoming = (
        db.session.execute(
            db.select(Activity)
            .where(Activity.status.in_([ActivityStatus.PUBLISHED, ActivityStatus.ONGOING]))
            .order_by(Activity.start_time.desc())
            .limit(4)
        )
        .scalars()
        .all()
    )

    published_count = db.session.execute(
        db.select(db.func.count())
        .select_from(Activity)
        .where(Activity.status.in_(ActivityStatus.VISIBLE_TO_STUDENT))
    ).scalar_one()

    return render_template(
        "main/index.html",
        upcoming=upcoming,
        published_count=published_count,
        now=now,
    )


@bp.get("/dashboard")
@login_required
def dashboard():
    """工作台。三种角色看到的内容不一样。"""
    context = {"stats": None}

    if current_user.is_club:
        activities = (
            db.session.execute(
                db.select(Activity).where(Activity.organizer_id == current_user.id)
            )
            .scalars()
            .all()
        )
        context["counts"] = _status_counts(activities)
        context["overview"] = _club_overview(activities)

    elif current_user.is_admin:
        context["pending_count"] = db.session.execute(
            db.select(db.func.count())
            .select_from(Activity)
            .where(Activity.status == ActivityStatus.PENDING)
        ).scalar_one()

    else:
        context["my_enrollments"] = (
            db.session.execute(
                db.select(Enrollment)
                .where(
                    Enrollment.user_id == current_user.id,
                    Enrollment.status == EnrollStatus.ENROLLED,
                )
                .order_by(Enrollment.enrolled_at.desc())
                .limit(5)
            )
            .scalars()
            .all()
        )

    return render_template("main/dashboard.html", **context)


def _status_counts(activities):
    """按状态统计数量，用于社团工作台的分组概览。"""
    counts = {status: 0 for status in ActivityStatus.ALL}
    for activity in activities:
        counts[activity.status] += 1
    return counts


def _club_overview(activities):
    total_enrolled = sum(a.enrolled_count for a in activities)
    total_checked = sum(activity_stats(a)["checked_in"] for a in activities)
    rate = round(total_checked * 100.0 / total_enrolled, 1) if total_enrolled else 0.0
    return {
        "activity_count": len(activities),
        "enrolled": total_enrolled,
        "checked_in": total_checked,
        "check_in_rate": rate,
    }


@bp.get("/club/")
@role_required(Role.CLUB)
def club_home():
    """社团工作台。

    这里刻意**不等于**管理员后台：管理员能管全部活动，但不借用社团身份去看
    社团的页面。权限边界一旦模糊，后面每加一个功能都要重新想一遍谁能用。
    """
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
        "main/club.html",
        activities=activities,
        counts=_status_counts(activities),
        overview=_club_overview(activities),
        status_labels=ActivityStatus.LABELS,
    )


@bp.get("/health")
def health():
    """健康检查，给部署脚本和冒烟测试用。"""
    return {"status": "ok", "authenticated": current_user.is_authenticated}
