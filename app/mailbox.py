"""站内邮件与公告。

两件事放在同一个入口（导航栏右上角的「邮件」）：

1. **公告**——首页那段"把校园活动从…搬到线上"的说明文字，原来硬编码在模板里，
   现在存进数据库（`Announcement` 单行表）。教师 / 管理员可以直接改，
   改完首页与邮件页同时生效；
2. **邮件**——**只有活动组织教师与系统管理员可以发送**，收件人按范围解析
   （全体用户 / 全体学生 / 全体教师 / 指定用户），学生在收件箱里读信。
   权限在服务端强制执行：学生连写邮件页都拿不到（403），前端藏按钮不算数。

一段话的原则：**发送方与"能改公告"的人一致**——公告本身就是一封写给所有人的信，
不该出现"能发信但不能改公告"的分裂。
"""

from flask import (
    Blueprint,
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
    Announcement,
    Message,
    MessageRecipient,
    Role,
    User,
    utcnow,
)
from .permissions import role_required

bp = Blueprint("mailbox", __name__, url_prefix="/mail")

# 公告的默认文案（首页说明文字）。空库 / 旧库第一次打开时按它建出公告行。
DEFAULT_ANNOUNCEMENT = (
    "把校园活动从「群通知 + 在线表格」搬到线上，跑通一条可追溯的闭环：\n"
    "组织教师发布 → 学生报名 / 候补 → 教师审核 → 现场签到 → 统计复盘\n"
    "\n"
    "V2.0 新增：满员不再「没下文」——报名自动进入候补队列，名额空出时按先来后到自动递补；"
    "需要审核的活动由组织教师逐人确认，通过不等于一定参加，还要看当时有没有位置。"
)

# 收件范围的取值与文案
SCOPES = {
    "all": "全体用户",
    "students": "全体学生",
    "teachers": "全体教师",
    "one": "指定用户",
}

SUBJECT_MAX_LENGTH = 100
ANNOUNCEMENT_MAX_LENGTH = 2000


# ---------------------------------------------------------------------------
# 公告
# ---------------------------------------------------------------------------


def get_announcement():
    """取公告（单行表）。

    没有就地补一条默认文案——旧库（比如线上沙箱里那份）加新表之后是空的，
    首页会没有说明文字，看起来像坏了。补默认值让升级"无感"。
    """
    row = db.session.get(Announcement, 1)
    if row is not None:
        return row

    row = Announcement(id=1, content=DEFAULT_ANNOUNCEMENT)
    db.session.add(row)
    try:
        db.session.commit()
    except Exception:
        # 并发下两条请求可能同时补——撞上唯一键就回滚重读，不是错误
        db.session.rollback()
        row = db.session.get(Announcement, 1)
    return row


def update_announcement(content, operator_id):
    """更新公告文字。返回 ``(是否成功, 错误说明)``。"""
    content = (content or "").strip()
    if not content:
        return False, "公告内容不能为空"
    if len(content) > ANNOUNCEMENT_MAX_LENGTH:
        return False, f"公告内容不能超过 {ANNOUNCEMENT_MAX_LENGTH} 字"

    row = get_announcement()
    row.content = content
    row.updated_by = operator_id
    row.updated_at = utcnow()
    db.session.commit()
    return True, None


# ---------------------------------------------------------------------------
# 邮件
# ---------------------------------------------------------------------------


def unread_count(user_id):
    """未读邮件数（导航栏的小红点）。"""
    return db.session.execute(
        db.select(db.func.count())
        .select_from(MessageRecipient)
        .where(
            MessageRecipient.user_id == user_id,
            MessageRecipient.read_at.is_(None),
        )
    ).scalar_one()


def resolve_recipients(scope, user_id=None, exclude_user_id=None):
    """把收件范围解析成收件人列表。

    :param scope: all / students / teachers / one
    :param user_id: scope 为 one 时必填
    :param exclude_user_id: 发件人不给自己发信
    :return: (收件人列表, 错误说明)
    """
    if scope not in SCOPES:
        return None, "请选择收件范围"

    query = db.select(User)
    if scope == "students":
        query = query.where(User.role == Role.STUDENT)
    elif scope == "teachers":
        query = query.where(User.role == Role.TEACHER)
    elif scope == "one":
        user = db.session.get(User, int(user_id)) if user_id else None
        if user is None:
            return None, "指定的收件人不存在"
        query = query.where(User.id == user.id)

    if exclude_user_id is not None:
        query = query.where(User.id != exclude_user_id)

    return db.session.execute(query.order_by(User.student_no)).scalars().all(), None


def send_message(subject, body, recipients, sender_id):
    """把一封邮件写进库：一条 Message + 每人一条 MessageRecipient。"""
    message = Message(sender_id=sender_id, subject=subject, body=body)
    db.session.add(message)
    db.session.flush()
    for person in recipients:
        db.session.add(MessageRecipient(message_id=message.id, user_id=person.id))
    db.session.commit()
    return message


def my_recipient_record(message_id, user_id):
    return db.session.execute(
        db.select(MessageRecipient).where(
            MessageRecipient.message_id == message_id,
            MessageRecipient.user_id == user_id,
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------


def _inbox(user_id):
    rows = db.session.execute(
        db.select(MessageRecipient, Message, User)
        .join(Message, Message.id == MessageRecipient.message_id)
        .join(User, User.id == Message.sender_id, isouter=True)
        .where(MessageRecipient.user_id == user_id)
        .order_by(Message.created_at.desc())
    ).all()
    return [
        {"record": record, "message": message, "sender": sender}
        for record, message, sender in rows
    ]


def _sent(user_id):
    rows = db.session.execute(
        db.select(Message, db.func.count(MessageRecipient.id))
        .join(MessageRecipient, MessageRecipient.message_id == Message.id)
        .where(Message.sender_id == user_id)
        .group_by(Message.id)
        .order_by(Message.created_at.desc())
    ).all()
    return [{"message": message, "recipient_count": count} for message, count in rows]


@bp.get("/")
@login_required
def index():
    """邮件页：公告 + 收件箱（+ 教师 / 管理员的发件箱与写作入口）。"""
    can_send = current_user.is_teacher or current_user.is_admin
    return render_template(
        "mailbox/index.html",
        announcement=get_announcement(),
        inbox=_inbox(current_user.id),
        sent=_sent(current_user.id) if can_send else [],
        can_send=can_send,
    )


@bp.get("/<int:message_id>")
@login_required
def detail(message_id):
    """信件详情。只有收件人本人或发件人能看。"""
    message = db.session.get(Message, message_id)
    if message is None:
        abort(404)

    record = my_recipient_record(message_id, current_user.id)
    is_sender = message.sender_id == current_user.id
    if record is None and not is_sender:
        # 不是你的信：当作不存在，别用 403 告诉对方"这封信存在"
        abort(404)

    if record is not None and record.read_at is None:
        record.read_at = utcnow()
        db.session.commit()

    return render_template(
        "mailbox/detail.html",
        message=message,
        is_sender=is_sender,
    )


@bp.route("/compose", methods=["GET", "POST"])
@role_required(Role.TEACHER, Role.ADMIN)
def compose():
    """写邮件。仅活动组织教师与系统管理员可用。"""
    if request.method == "POST":
        scope = (request.form.get("scope") or "").strip()
        subject = (request.form.get("subject") or "").strip()
        body = (request.form.get("body") or "").strip()
        target_id = request.form.get("user_id", type=int)

        error = None
        if not subject:
            error = "主题不能为空"
        elif len(subject) > SUBJECT_MAX_LENGTH:
            error = f"主题不能超过 {SUBJECT_MAX_LENGTH} 字"
        elif not body:
            error = "正文不能为空"
        else:
            recipients, error = resolve_recipients(
                scope, user_id=target_id, exclude_user_id=current_user.id
            )
            if error is None and not recipients:
                error = "这个范围内没有可发送的收件人"

        if error:
            flash(error, "danger")
            return (
                render_template(
                    "mailbox/compose.html",
                    scopes=SCOPES,
                    users=_sendable_users(),
                    form={"scope": scope, "subject": subject, "body": body,
                          "user_id": target_id},
                ),
                400,
            )

        message = send_message(subject, body, recipients, current_user.id)
        flash(
            f"已发送「{message.subject}」，共送达 {len(recipients)} 人。",
            "success",
        )
        return redirect(url_for("mailbox.index"))

    return render_template(
        "mailbox/compose.html",
        scopes=SCOPES,
        users=_sendable_users(),
        form={"scope": "students", "subject": "", "body": "", "user_id": None},
    )


def _sendable_users():
    """指定收件人下拉框里的人（按学号排序，排除自己）。"""
    return (
        db.session.execute(
            db.select(User)
            .where(User.id != current_user.id)
            .order_by(User.role, User.student_no)
        )
        .scalars()
        .all()
    )


@bp.post("/announcement")
@role_required(Role.TEACHER, Role.ADMIN)
def edit_announcement():
    """更新首页公告（教师 / 管理员）。"""
    ok, error = update_announcement(
        request.form.get("content"), current_user.id
    )
    if ok:
        flash("公告已更新，首页与邮件页同时生效。", "success")
    else:
        flash(error, "danger")
    return redirect(url_for("mailbox.index"))
