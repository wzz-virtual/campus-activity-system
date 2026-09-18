"""数据模型。

对应设计方案 docs/mvp-design.md 第 5 节，共五张表：
user / category / activity / enrollment / audit_log

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
    """用户角色。"""

    STUDENT = "STUDENT"
    CLUB = "CLUB"
    ADMIN = "ADMIN"

    ALL = (STUDENT, CLUB, ADMIN)

    LABELS = {
        STUDENT: "学生",
        CLUB: "社团",
        ADMIN: "校级管理员",
    }


class ActivityStatus:
    """活动状态机。流转规则见 docs/mvp-design.md 第 3 节。"""

    DRAFT = "DRAFT"
    PENDING = "PENDING"
    REJECTED = "REJECTED"
    PUBLISHED = "PUBLISHED"
    ONGOING = "ONGOING"
    FINISHED = "FINISHED"
    CANCELED = "CANCELED"

    ALL = (DRAFT, PENDING, REJECTED, PUBLISHED, ONGOING, FINISHED, CANCELED)

    LABELS = {
        DRAFT: "草稿",
        PENDING: "待审核",
        REJECTED: "已驳回",
        PUBLISHED: "已发布",
        ONGOING: "进行中",
        FINISHED: "已结束",
        CANCELED: "已取消",
    }

    # 学生能看到的活动状态
    VISIBLE_TO_STUDENT = (PUBLISHED, ONGOING, FINISHED, CANCELED)

    # 允许学生报名的状态
    ENROLLABLE = (PUBLISHED,)


class EnrollStatus:
    """报名记录状态。"""

    ENROLLED = "ENROLLED"
    CANCELED = "CANCELED"

    ALL = (ENROLLED, CANCELED)


class AuditAction:
    """审核日志的动作类型。"""

    SUBMIT = "SUBMIT"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    CANCEL = "CANCEL"


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
    # 仅社团角色使用
    club_name = db.Column(db.String(100))
    # 1 正常 / 0 停用
    status = db.Column(db.SmallInteger, nullable=False, default=1)
    # 连续登录失败次数，登录成功即清零。用于抵挡撞库（暴力猜密码）。
    failed_login_count = db.Column(db.SmallInteger, nullable=False, default=0)
    # 锁定到期时间，NULL 表示未锁定。到点自动放行，不需要管理员手工解锁。
    locked_until = db.Column(db.DateTime)
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
    def is_club(self):
        return self.role == Role.CLUB

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
    # 人数上限
    capacity = db.Column(db.Integer, nullable=False)
    # 冗余计数：它不是缓存，而是并发控制的载体。详见设计方案第 6.1 节
    enrolled_count = db.Column(db.Integer, nullable=False, default=0)
    enroll_deadline = db.Column(db.DateTime, nullable=False)
    status = db.Column(
        db.String(20), nullable=False, default=ActivityStatus.DRAFT, index=True
    )
    reject_reason = db.Column(db.String(255))
    approved_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    approved_at = db.Column(db.DateTime)
    # 开启签到时生成的 6 位签到码
    check_in_code = db.Column(db.String(6))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    category = db.relationship("Category", back_populates="activities")
    organizer = db.relationship(
        "User", foreign_keys=[organizer_id], back_populates="organized_activities"
    )
    approver = db.relationship("User", foreign_keys=[approved_by])
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
    """报名记录。

    签到信息直接挂在这张表上，不单独建 attendance 表——MVP 不支持
    「未报名直接签到」，两者是一对一关系，合表更简单。
    """

    __tablename__ = "enrollment"

    id = db.Column(db.Integer, primary_key=True)
    activity_id = db.Column(
        db.Integer, db.ForeignKey("activity.id"), nullable=False
    )
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    status = db.Column(
        db.String(20), nullable=False, default=EnrollStatus.ENROLLED
    )
    enrolled_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    canceled_at = db.Column(db.DateTime)
    check_in_at = db.Column(db.DateTime)
    # 手动核验签到的人（组织者或管理员）
    check_in_by = db.Column(db.Integer, db.ForeignKey("user.id"))

    activity = db.relationship(
        "Activity", foreign_keys=[activity_id], back_populates="enrollments"
    )
    user = db.relationship(
        "User", foreign_keys=[user_id], back_populates="enrollments"
    )
    check_in_operator = db.relationship("User", foreign_keys=[check_in_by])

    __table_args__ = (
        # 唯一索引：同一个人对同一个活动只能有一条报名记录。
        # 这是防重复报名和防并发超卖的最后一道防线，不能省。
        db.UniqueConstraint("activity_id", "user_id", name="uk_activity_user"),
        db.Index("idx_activity_status", "activity_id", "status"),
    )

    @property
    def has_checked_in(self):
        return self.check_in_at is not None

    def __repr__(self):
        return f"<Enrollment a={self.activity_id} u={self.user_id} {self.status}>"


class AuditLog(db.Model):
    """活动审核日志。只追加，不修改。"""

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

    def __repr__(self):
        return f"<AuditLog a={self.activity_id} {self.action}>"
