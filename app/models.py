"""数据模型（V2.0）。

对应《实验2-需求分析.md》第四章「数据实体建议」，共六张表：
user / category / activity / enrollment / audit_log / password_reset_request

V2.0 相对 V1.0 的变化：
1. 角色由「学生 / 社团 / 校级管理员」改为「学生 / **活动组织教师** / 系统管理员」；
2. 活动去掉了「提交审核 / 通过 / 驳回」流程——教师直接发布，
   管理员改为事后监督（下线 / 恢复展示）；
3. 报名记录由两态扩展为**五态**（待审核 / 正式 / 候补 / 已拒绝 / 已取消），
   新增「进入候补时间」与「审核时间」，支撑候补队列与报名审核。

约定：
1. 所有时间字段统一存 **UTC**，展示层再转本地时区。
2. 角色、状态这类取值用「字符串常量」而不是数据库 ENUM，
   这样加一个新状态只需要改代码，不用改表结构。
"""

from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .config import MIN_PASSWORD_LENGTH
from .extensions import db


def utcnow():
    """返回不含时区信息的 UTC 当前时间。

    数据库里统一存 UTC。SQLite 不保存时区信息，所以这里显式去掉 tzinfo，
    避免同一列里读出「有时区」和「无时区」两种值，导致比较时抛 TypeError。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Role:
    """用户角色。

    V2.0 的三类角色（见需求分析 1.3 与 3.1 权限矩阵）：

    * 学生 —— 浏览、报名 / 候补、取消本人报名；
    * 活动组织教师 —— 创建 / 发布 / 修改**本人负责**的活动，审核报名，管理名单；
    * 系统管理员 —— 维护账号（停用 / 恢复）、监督活动（下线 / 恢复），
      **不介入**具体报名、不代替教师编辑活动内容。
    """

    STUDENT = "STUDENT"
    TEACHER = "TEACHER"
    ADMIN = "ADMIN"

    ALL = (STUDENT, TEACHER, ADMIN)

    LABELS = {
        STUDENT: "学生",
        TEACHER: "活动组织教师",
        ADMIN: "系统管理员",
    }


class ActivityStatus:
    """活动状态。

    V2.0 **去掉了活动审核流**（V1.0 的「待审核 / 已驳回」不再存在）：
    教师创建后直接发布，管理员的职责改为对违规活动的**下线 / 恢复**。

    时间推进（`PUBLISHED → ONGOING → FINISHED`）的规则和 V1.0 一致，
    见 ``services.sync_activity_statuses``。
    """

    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    ONGOING = "ONGOING"
    FINISHED = "FINISHED"
    # 管理员对违规或明显错误的活动做状态处理（A-07）
    OFFLINE = "OFFLINE"
    CANCELED = "CANCELED"

    ALL = (DRAFT, PUBLISHED, ONGOING, FINISHED, OFFLINE, CANCELED)

    LABELS = {
        DRAFT: "草稿",
        PUBLISHED: "已发布",
        ONGOING: "进行中",
        FINISHED: "已结束",
        OFFLINE: "已下线",
        CANCELED: "已取消",
    }

    # 学生能看到的活动状态。**已下线不在其中**：被处理的活动对
    # 学生「不展示、不能报名」（TC-14），但对教师和创建者仍然可见。
    VISIBLE_TO_STUDENT = (PUBLISHED, ONGOING, FINISHED, CANCELED)

    # 允许学生报名的状态
    ENROLLABLE = (PUBLISHED,)

    # 管理员可以执行「下线」的状态（草稿本就不对外，已取消没有必要）
    OFFINABLE = (PUBLISHED, ONGOING, FINISHED)


class EnrollStatus:
    """报名记录状态。

    V2.0 五态（需求分析 4.3 的「状态词表」）：

    * ``PENDING``    待审核 —— 需审核的活动，报名后的初始态；**不占**名额
    * ``CONFIRMED``  正式   —— 占用一个名额
    * ``WAITLISTED`` 候补   —— 未获得名额，按「进入候补时间」排队；**不占**名额
    * ``REJECTED``   已拒绝 —— 终态，不符合参加条件
    * ``CANCELED``   已取消 —— 终态，学生取消 / 撤销 / 退出

    「待审核 / 正式 / 候补」合称**有效报名**：同一学生对同一活动最多一条
    （BR-2）。「已拒绝 / 已取消」之后可以重新报名，但按**新的**时间重新排队
    （DEC-3）。
    """

    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    WAITLISTED = "WAITLISTED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"

    ALL = (PENDING, CONFIRMED, WAITLISTED, REJECTED, CANCELED)

    LABELS = {
        PENDING: "待审核",
        CONFIRMED: "正式",
        WAITLISTED: "候补",
        REJECTED: "已拒绝",
        CANCELED: "已取消",
    }

    # 有效报名：处于其中任一状态即为「已报名」，不可重复报名
    ACTIVE = (PENDING, CONFIRMED, WAITLISTED)

    # 占用活动正式名额的状态（BR-1 约束的对象）
    OCCUPYING = (CONFIRMED,)


class AuditAction:
    """活动流转日志的动作类型。

    V2.0 记录四类活动级事件：发布 / 下线 / 恢复 / 取消。
    报名审核的结果记在 `enrollment.reviewed_at` 上，不进这张表。
    """

    PUBLISH = "PUBLISH"
    OFFLINE = "OFFLINE"
    RESTORE = "RESTORE"
    CANCEL = "CANCEL"

    # V1.0 活动审核流留下的历史动作。库里可能还有这样的旧日志行，
    # 保留常量只是为了把它们渲染成人能看懂的字，新代码不再产生。
    LEGACY_SUBMIT = "SUBMIT"
    LEGACY_APPROVE = "APPROVE"
    LEGACY_REJECT = "REJECT"

    LABELS = {
        PUBLISH: "发布活动",
        OFFLINE: "下线活动",
        RESTORE: "恢复展示",
        CANCEL: "取消活动",
        LEGACY_SUBMIT: "提交审核（V1.0 遗留）",
        LEGACY_APPROVE: "审核通过（V1.0 遗留）",
        LEGACY_REJECT: "审核驳回（V1.0 遗留）",
    }


class ResetRequestStatus:
    """忘记密码申请的处理状态。"""

    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"

    ALL = (PENDING, RESOLVED, REJECTED)

    LABELS = {
        PENDING: "待处理",
        RESOLVED: "已重置",
        REJECTED: "未通过",
    }


class User(UserMixin, db.Model):
    """用户。三种角色共用一张表，靠 role 字段区分。"""

    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    # 学号 / 工号，同时作为登录账号
    student_no = db.Column(db.String(20), unique=True, nullable=False, index=True)
    name = db.Column(db.String(50), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(100))
    phone = db.Column(db.String(20))
    role = db.Column(db.String(20), nullable=False, default=Role.STUDENT, index=True)
    # 所属组织 / 部门（教师账号用来显示「张老师 · 摄影协会」这样的归属）
    club_name = db.Column(db.String(100))
    # 1 正常 / 0 停用
    status = db.Column(db.SmallInteger, nullable=False, default=1)
    # 连续登录失败次数，登录成功即清零。用于抵挡撞库（暴力猜密码）。
    failed_login_count = db.Column(db.SmallInteger, nullable=False, default=0)
    # 锁定到期时间，NULL 表示未锁定。到点自动放行，不需要管理员手工解锁。
    locked_until = db.Column(db.DateTime)
    # 管理员重置密码后置 1，强制本人下次登录后立即修改
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    enrollments = db.relationship(
        "Enrollment",
        foreign_keys="Enrollment.user_id",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    organized_activities = db.relationship(
        "Activity",
        foreign_keys="Activity.organizer_id",
        back_populates="organizer",
    )

    def set_password(self, raw_password):
        """保存密码的哈希值，绝不存明文。

        顺手卡一次最小长度。密码策略必须落在「写入」这一个必经点上，
        否则将来加了改密码、管理员重置密码之类的入口，很容易漏掉校验。
        """
        if len(raw_password or "") < MIN_PASSWORD_LENGTH:
            raise ValueError(f"密码长度不能少于 {MIN_PASSWORD_LENGTH} 个字符")
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        """校验密码。用哈希比对，不是字符串相等。"""
        return check_password_hash(self.password_hash, raw_password)

    # ---- 登录失败限制 ----

    def is_locked(self, now=None):
        """账号当前是否处于锁定期。"""
        if self.locked_until is None:
            return False
        return (now or utcnow()) < self.locked_until

    def lock_remaining_seconds(self, now=None):
        """距离自动解锁还有多少秒；未锁定返回 0。"""
        if not self.is_locked(now):
            return 0
        delta = self.locked_until - (now or utcnow())
        return max(int(delta.total_seconds()), 0)

    def register_failed_login(self, max_attempts, lock_duration):
        """记一次登录失败；达到阈值就锁定一段时间。调用方负责 commit。"""
        self.failed_login_count = (self.failed_login_count or 0) + 1
        if self.failed_login_count >= max_attempts:
            self.locked_until = utcnow() + lock_duration
            # 计数归零：解锁后重新给满次数，否则下一次失败会立刻又锁上
            self.failed_login_count = 0

    def reset_login_failures(self):
        """登录成功后清空失败记录。"""
        self.failed_login_count = 0
        self.locked_until = None

    @property
    def is_active(self):
        """Flask-Login 会读这个属性来决定账号能否登录。"""
        return self.status == 1

    @property
    def role_label(self):
        return Role.LABELS.get(self.role, self.role)

    @property
    def is_admin(self):
        return self.role == Role.ADMIN

    @property
    def is_teacher(self):
        return self.role == Role.TEACHER

    @property
    def is_student(self):
        return self.role == Role.STUDENT

    def __repr__(self):
        return f"<User {self.student_no} {self.name} {self.role}>"

    def to_dict(self):
        return {
            "id": self.id,
            "student_no": self.student_no,
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "role": self.role,
            "role_label": self.role_label,
            "club_name": self.club_name,
        }


class Category(db.Model):
    """活动分类。只有管理员能维护。"""

    __tablename__ = "category"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(30), unique=True, nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    activities = db.relationship("Activity", back_populates="category")

    def __repr__(self):
        return f"<Category {self.name}>"


class Activity(db.Model):
    """活动。"""

    __tablename__ = "activity"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    cover_url = db.Column(db.String(255))
    # 纯文本简介，不做富文本
    description = db.Column(db.Text)
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"), index=True)
    # 创建人（社团或管理员）
    organizer_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False, index=True
    )
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    location = db.Column(db.String(150))
    # 人数上限：活动最多可接待的**正式参加**人数（T-03）
    capacity = db.Column(db.Integer, nullable=False)
    # 冗余计数：它不是缓存，而是并发控制的载体。详见设计方案第 6.1 节。
    # 口径 = 状态为「正式」的报名数；候选（候补）不计入。
    enrolled_count = db.Column(db.Integer, nullable=False, default=0)
    enroll_deadline = db.Column(db.DateTime, nullable=False)
    # 是否需要组织教师逐人审核报名（T-13、T-14）。
    # 关掉时：有余位直接「正式」，满员直接进「候补」。
    requires_review = db.Column(db.Boolean, nullable=False, default=False)
    # 参加资格说明。需审核的活动应当填写，供学生在报名前了解条件（T-13）。
    qualification = db.Column(db.String(255))
    status = db.Column(
        db.String(20), nullable=False, default=ActivityStatus.DRAFT, index=True
    )
    # 开启签到时生成的 6 位签到码
    check_in_code = db.Column(db.String(6))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    category = db.relationship("Category", back_populates="activities")
    organizer = db.relationship(
        "User", foreign_keys=[organizer_id], back_populates="organized_activities"
    )
    enrollments = db.relationship(
        "Enrollment",
        foreign_keys="Enrollment.activity_id",
        back_populates="activity",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        db.Index("idx_status_start", "status", "start_time"),
        db.CheckConstraint("capacity > 0", name="ck_activity_capacity_positive"),
        db.CheckConstraint(
            "enrolled_count >= 0", name="ck_activity_enrolled_non_negative"
        ),
    )

    @property
    def remaining(self):
        """剩余名额。"""
        return max(self.capacity - self.enrolled_count, 0)

    @property
    def is_full(self):
        return self.enrolled_count >= self.capacity

    @property
    def status_label(self):
        return ActivityStatus.LABELS.get(self.status, self.status)

    def __repr__(self):
        return f"<Activity {self.id} {self.title} {self.status}>"


class Enrollment(db.Model):
    """报名记录。V2.0 的五态见 `EnrollStatus`。

    一条记录的生命周期可能是「不止一次报名」的：取消 / 被拒之后重新报名时
    **复用同一条记录**（唯一索引 `uk_activity_user` 决定了同一个人对同一个
    活动只能有一条），只是把状态和新一轮的时间戳覆盖上去。

    候补队列的排序**只看 `waitlisted_at`**（BR-4）——不存"排位"字段，
    排位是查询时按时间算出来的。这样"候补者退出后，其后人员自动前移"
    （BR-7）是天然成立的：少了一个人，后面每个人的排位自然就前进了，
    不需要（也没有入口）去改任何人的顺序。
    """

    __tablename__ = "enrollment"

    id = db.Column(db.Integer, primary_key=True)
    activity_id = db.Column(
        db.Integer, db.ForeignKey("activity.id"), nullable=False
    )
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    status = db.Column(
        db.String(20), nullable=False, default=EnrollStatus.CONFIRMED
    )
    # 报名（提交申请）的时刻。重新报名时更新为新一轮的时间。
    enrolled_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    # 进入候补队列的时刻。**候补排序的唯一依据**，只在状态变为候补时写入。
    # V3.0 起它同时是「候补历史」的痕迹：退出候补 / 转正后都不再清空
    # （队列查询按 status 过滤，不受影响），用于统计该活动的候补转正率（DR-4）。
    waitlisted_at = db.Column(db.DateTime)
    # V3.0（DR-4）：候补转正式的时刻。NULL = 从没经历过「候补→正式」。
    # 直接报名就转正式的不算（没有"候补"这段经历）。
    promoted_at = db.Column(db.DateTime)
    # 教师审核的时刻与操作人（仅"需审核的活动"会填写）
    reviewed_at = db.Column(db.DateTime)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    canceled_at = db.Column(db.DateTime)
    check_in_at = db.Column(db.DateTime)
    # 手动核验签到的人（组织者）
    check_in_by = db.Column(db.Integer, db.ForeignKey("user.id"))

    activity = db.relationship(
        "Activity", foreign_keys=[activity_id], back_populates="enrollments"
    )
    user = db.relationship(
        "User", foreign_keys=[user_id], back_populates="enrollments"
    )
    check_in_operator = db.relationship("User", foreign_keys=[check_in_by])
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])

    __table_args__ = (
        # 唯一索引：同一个人对同一个活动只能有一条报名记录。
        # 这是防重复报名和防并发超卖的最后一道防线，不能省。
        db.UniqueConstraint("activity_id", "user_id", name="uk_activity_user"),
        db.Index("idx_activity_status", "activity_id", "status"),
    )

    @property
    def status_label(self):
        return EnrollStatus.LABELS.get(self.status, self.status)

    @property
    def is_active(self):
        """是否为「有效报名」（待审核 / 正式 / 候补）。"""
        return self.status in EnrollStatus.ACTIVE

    @property
    def has_checked_in(self):
        return self.check_in_at is not None

    def __repr__(self):
        return f"<Enrollment a={self.activity_id} u={self.user_id} {self.status}>"


class AuditLog(db.Model):
    """活动流转日志。只追加，不修改。

    V2.0 记录：发布 / 管理员下线（含原因）/ 恢复 / 取消。
    「谁在什么时候把哪场活动下线了、为什么」是 TC-14 要求教师端能看到的
    「处理标记」的数据来源。
    """

    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    activity_id = db.Column(
        db.Integer, db.ForeignKey("activity.id"), nullable=False, index=True
    )
    operator_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    action = db.Column(db.String(20), nullable=False)
    reason = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    operator = db.relationship("User", foreign_keys=[operator_id])
    activity = db.relationship("Activity", foreign_keys=[activity_id])

    def __repr__(self):
        return f"<AuditLog a={self.activity_id} {self.action}>"


class Message(db.Model):
    """站内邮件（一封信）。

    V2.0 新增的「邮件功能」：**只有活动组织教师与系统管理员可以发送**，
    面向其他用户（全体 / 全体学生 / 全体教师 / 指定用户）。
    学生只能收信——这个限制在服务端强制执行，前端藏按钮不算数。

    收件人在 `MessageRecipient` 里逐人展开：发信时按范围解析成一条条
    收件记录，每个人各自持有"已读 / 未读"，互不影响。
    """

    __tablename__ = "message"

    id = db.Column(db.Integer, primary_key=True)
    # 发件人。理论上有值；置空仅用于将来"系统通知"的场景。
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    subject = db.Column(db.String(100), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    sender = db.relationship("User", foreign_keys=[sender_id])
    recipients = db.relationship(
        "MessageRecipient",
        back_populates="message",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<Message {self.id} {self.subject!r}>"


class MessageRecipient(db.Model):
    """一封信与一位收件人的对应关系（含已读时间）。"""

    __tablename__ = "message_recipient"

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(
        db.Integer, db.ForeignKey("message.id"), nullable=False
    )
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    # NULL = 未读。置上时间即为"读过"的时刻。
    read_at = db.Column(db.DateTime)

    message = db.relationship("Message", back_populates="recipients")
    user = db.relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        # 同一个人对同一封信只能有一条收件记录
        db.UniqueConstraint("message_id", "user_id", name="uk_message_user"),
        # 未读计数走这个索引：where user_id = ? and read_at is null
        db.Index("idx_recipient_user_read", "user_id", "read_at"),
    )

    def __repr__(self):
        return f"<MessageRecipient m={self.message_id} u={self.user_id}>"


class Announcement(db.Model):
    """首页公告（单行表：永远只有 id=1 这一行）。

    这段文字原来硬编码在首页模板里，现在搬进数据库：教师 / 管理员
    可以在「邮件」页面直接改，改完首页与邮件页同时生效。
    空库或旧库第一次打开首页时，会按默认文案自动建出这一行。
    """

    __tablename__ = "announcement"

    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    updated_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    updated_at = db.Column(db.DateTime)

    editor = db.relationship("User", foreign_keys=[updated_by])

    def __repr__(self):
        return f"<Announcement {len(self.content or '')} chars>"


class PasswordResetRequest(db.Model):
    """忘记密码的找回申请。

    流程：学生提交申请 → 管理员核实身份 → 重置为随机临时密码并告知 →
    学生用临时密码登录后被强制修改密码（`User.must_change_password`）。

    **为什么不让管理员直接指定新密码**：那样管理员就知道了用户的密码，
    账号就失去了「只有本人知道密码」这个最基本的属性，出了问题也说不清
    是谁在用这个账号。系统只生成随机临时密码，并要求本人登录后立即改掉。
    """

    __tablename__ = "password_reset_request"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    # 学生自述的佐证信息（院系班级、联系方式等），供管理员核实身份
    note = db.Column(db.String(255))
    status = db.Column(
        db.String(20), nullable=False, default=ResetRequestStatus.PENDING
    )
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    handled_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    handled_at = db.Column(db.DateTime)
    # 管理员处理时的备注，例如"已电话核实"
    handle_note = db.Column(db.String(255))

    # 一张表里有两个指向 user 的外键，必须显式指明各自用哪一个
    user = db.relationship("User", foreign_keys=[user_id])
    handler = db.relationship("User", foreign_keys=[handled_by])

    __table_args__ = (
        db.Index("idx_reset_status_created", "status", "created_at"),
    )

    @property
    def status_label(self):
        return ResetRequestStatus.LABELS.get(self.status, self.status)

    def __repr__(self):
        return f"<PasswordResetRequest u={self.user_id} {self.status}>"
