"""主页面路由：首页、工作台、教师工作台、健康检查。"""

from flask import Blueprint, render_template
from flask_login import current_user, login_required

from .extensions import db
from .models import (
    Activity,
    ActivityStatus,
    EnrollStatus,
    Enrollment,
    Role,
    User,
    utcnow,
)
from .permissions import role_required
from .services import (
    activity_stats,
    pending_count,
    waitlist_count,
    waitlist_position,
)

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
    context = {}

    if current_user.is_teacher:
        activities = (
            db.session.execute(
                db.select(Activity).where(Activity.organizer_id == current_user.id)
            )
            .scalars()
            .all()
        )
        context["counts"] = _status_counts(activities)
        context["overview"] = _teacher_overview(activities)
        # 有人等着处理时，工作台上要第一眼看见
        context["pending_total"] = sum(pending_count(a.id) for a in activities)
        context["waitlist_total"] = sum(waitlist_count(a.id) for a in activities)

    elif current_user.is_admin:
        context["user_count"] = db.session.execute(
            db.select(db.func.count()).select_from(User)
        ).scalar_one()
        context["offline_count"] = db.session.execute(
            db.select(db.func.count())
            .select_from(Activity)
            .where(Activity.status == ActivityStatus.OFFLINE)
        ).scalar_one()

    else:
        context["my_enrollments"] = (
            db.session.execute(
                db.select(Enrollment)
                .where(
                    Enrollment.user_id == current_user.id,
                    Enrollment.status.in_(EnrollStatus.ACTIVE),
                )
                .order_by(Enrollment.enrolled_at.desc())
                .limit(5)
            )
            .scalars()
            .all()
        )
        context["waitlist_positions"] = {
            row.id: waitlist_position(row)
            for row in context["my_enrollments"]
            if row.status == EnrollStatus.WAITLISTED
        }

    return render_template("main/dashboard.html", **context)


def _status_counts(activities):
    """按状态统计数量，用于教师工作台的分组概览。"""
    counts = {status: 0 for status in ActivityStatus.ALL}
    for activity in activities:
        counts[activity.status] += 1
    return counts


def _teacher_overview(activities):
    total_enrolled = sum(a.enrolled_count for a in activities)
    total_checked = sum(activity_stats(a)["checked_in"] for a in activities)
    rate = round(total_checked * 100.0 / total_enrolled, 1) if total_enrolled else 0.0
    return {
        "activity_count": len(activities),
        "enrolled": total_enrolled,
        "checked_in": total_checked,
        "check_in_rate": rate,
    }


@bp.get("/teacher/")
@role_required(Role.TEACHER)
def teacher_home():
    """教师工作台。

    这里刻意**不等于**管理员后台：管理员能监督全部活动，但不借用教师的身份
    去看教师的报名名单。权限边界一旦模糊，后面每加一个功能都要重新想一遍谁能用。
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
        "main/teacher.html",
        activities=activities,
        counts=_status_counts(activities),
        overview=_teacher_overview(activities),
        status_labels=ActivityStatus.LABELS,
        pending_counts={a.id: pending_count(a.id) for a in activities},
        waitlist_counts={a.id: waitlist_count(a.id) for a in activities},
    )


@bp.get("/health")
def health():
    """健康检查，给部署脚本和冒烟测试用。"""
    return {"status": "ok", "authenticated": current_user.is_authenticated}
