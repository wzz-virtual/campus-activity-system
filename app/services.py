"""业务规则层。

把规则集中在这里、而不是散落在各个视图里，有三个理由：

1. **同一条规则会被多个入口用到**。比如「能不能报名」既要在详情页决定按钮
   状态，也要在真正提交报名时再判一次。写成两份，迟早会不一致。
2. **并发控制必须是原子的**。扣名额的写法一旦被复制到别处，很容易被写成
   「先查人数、再插入」——那正是这类系统最经典的超卖缺陷。
3. **测试可以直接测规则本身**，不必每次都绕经 HTTP 请求。

规则出处是 ``docs/product-design.md`` 第 5 节（业务规则汇总，单一事实来源），
实现方式见 ``docs/mvp-design.md`` 第 6 节。
"""

import secrets
from datetime import timedelta

from sqlalchemy.exc import IntegrityError

from .extensions import db
from .models import (
    Activity,
    ActivityStatus,
    AuditAction,
    AuditLog,
    EnrollStatus,
    Enrollment,
    utcnow,
)
from .timeutils import parse_local

# ---------------------------------------------------------------------------
# 业务常量：调规则只改这里
# ---------------------------------------------------------------------------

# 取消报名的锁定窗口。距报名截止不足这个时长起，名单锁定、不可再取消。
# 取 24 小时是「给组织者留出按名单备料的时间」与「给学生留出反悔空间」的折中。
CANCEL_LOCK_WINDOW = timedelta(hours=24)

# 界面上要写「不足 24 小时」，所以这里再给一个整小时数。
# 别让界面把 24 这个数字硬编码一遍——改窗口时漏改一处，
# 提示语就会和真实规则对不上，而这种错没人会主动去核对。
CANCEL_LOCK_HOURS = int(CANCEL_LOCK_WINDOW.total_seconds() // 3600)

CANCEL_LOCK_MESSAGE = f"距报名截止不足 {CANCEL_LOCK_HOURS} 小时，名单已锁定"

# 签到码位数
CHECK_IN_CODE_LENGTH = 6

# 活动表单的字段长度上限，与 models.py 里的列定义保持一致
TITLE_MAX_LENGTH = 100
LOCATION_MAX_LENGTH = 150
COVER_URL_MAX_LENGTH = 255

# 允许「编辑」的状态。
#
# 注意这里是比产品文档 F2 更严的一版，依据是 product-design.md 第 5.1 节的
# 状态表（该节被明确声明为单一事实来源）：`ONGOING` 只允许签到与核验，
# `FINISHED` 只允许查看统计，`CANCELED` 更是全只读。
# 因此「进行中/已结束后核心信息不可修改」这句在实现上落成了「整条活动不可编辑」。
EDITABLE_STATUSES = (
    ActivityStatus.DRAFT,
    ActivityStatus.PENDING,
    ActivityStatus.REJECTED,
    ActivityStatus.PUBLISHED,
)

# 社团能编辑的状态：仅限「本人创建且未发布」。管理员不受此限。
CLUB_EDITABLE_STATUSES = (
    ActivityStatus.DRAFT,
    ActivityStatus.PENDING,
    ActivityStatus.REJECTED,
)

# 允许取消活动的状态：状态机里「未结束前任意阶段 ──> 已取消」
CANCELABLE_STATUSES = (
    ActivityStatus.DRAFT,
    ActivityStatus.PENDING,
    ActivityStatus.REJECTED,
    ActivityStatus.PUBLISHED,
    ActivityStatus.ONGOING,
)

# 能提交审核的状态：草稿首次提交，或驳回后修改了重新提交
SUBMITTABLE_STATUSES = (ActivityStatus.DRAFT, ActivityStatus.REJECTED)

REJECT_REASON_MAX_LENGTH = 255


# ---------------------------------------------------------------------------
# 活动状态机
# ---------------------------------------------------------------------------


def generate_check_in_code():
    """生成 6 位数字签到码。

    用 ``secrets`` 而不是 ``random``：签到码决定谁能签到，
    ``random`` 基于可预测的伪随机序列，知道几个输出就能推出后续输出。
    """
    return "".join(secrets.choice("0123456789") for _ in range(CHECK_IN_CODE_LENGTH))


def sync_activity_statuses(now=None):
    """按时间把活动推进到下一个状态。

    `PUBLISHED` 到点变 `ONGOING`，`ONGOING` 到点变 `FINISHED`。

    **为什么用「每次请求顺手同步」而不是定时任务**：MVP 只跑一个进程、
    没有独立的调度器，加一个 crontab 会多出一整套运维依赖。而状态本身
    完全由时间决定，读的时候现算一次即可，不存在算错的可能。

    代价是每次请求多两条查询。对单校区、百级并发的规模可以忽略。

    :param now: 便于测试注入一个固定的「当前时间」
    :return: 是否有活动发生了变化
    """
    now = now or utcnow()
    changed = False

    started = (
        db.session.execute(
            db.select(Activity).where(
                Activity.status == ActivityStatus.PUBLISHED,
                Activity.start_time <= now,
            )
        )
        .scalars()
        .all()
    )
    for activity in started:
        activity.status = ActivityStatus.ONGOING
        # 签到码在进入「进行中」时生成。已经有码就不动，
        # 免得把已经展示给学生的码换掉。
        if not activity.check_in_code:
            activity.check_in_code = generate_check_in_code()
        changed = True

    finished = (
        db.session.execute(
            db.select(Activity).where(
                Activity.status == ActivityStatus.ONGOING,
                Activity.end_time <= now,
            )
        )
        .scalars()
        .all()
    )
    for activity in finished:
        activity.status = ActivityStatus.FINISHED
        changed = True

    if changed:
        db.session.commit()
    return changed


def _log(activity, operator_id, action, reason=None):
    """记一条审核日志。这张表只追加、不修改。"""
    db.session.add(
        AuditLog(
            activity_id=activity.id,
            operator_id=operator_id,
            action=action,
            reason=(reason or None) and reason[:REJECT_REASON_MAX_LENGTH],
        )
    )


def submit_for_review(activity, operator_id):
    """提交审核（草稿首次提交，或驳回后重新提交）。"""
    if activity.status not in SUBMITTABLE_STATUSES:
        return False, f"当前状态（{activity.status_label}）不能提交审核"

    activity.status = ActivityStatus.PENDING
    _log(activity, operator_id, AuditAction.SUBMIT)
    db.session.commit()
    return True, None


def withdraw_to_draft(activity):
    """在待审核阶段撤回为草稿。"""
    if activity.status != ActivityStatus.PENDING:
        return False, "只有待审核的活动可以撤回"

    activity.status = ActivityStatus.DRAFT
    db.session.commit()
    return True, None


def approve_activity(activity, admin_id):
    """审核通过：状态变为已发布，学生立即可见。"""
    if activity.status != ActivityStatus.PENDING:
        return False, "只有待审核的活动可以审核"

    activity.status = ActivityStatus.PUBLISHED
    activity.approved_by = admin_id
    activity.approved_at = utcnow()
    # 驳回原因保留在库里做历史，界面上只在「已驳回」状态展示
    _log(activity, admin_id, AuditAction.APPROVE)
    db.session.commit()
    return True, None


def reject_activity(activity, admin_id, reason):
    """审核驳回。

    **驳回必须填原因**：不填原因的驳回等于让组织者猜。猜不出来就会来问，
    审核环节的沟通成本反而更高。
    """
    if activity.status != ActivityStatus.PENDING:
        return False, "只有待审核的活动可以驳回"

    reason = (reason or "").strip()
    if not reason:
        return False, "驳回时必须填写原因"
    if len(reason) > REJECT_REASON_MAX_LENGTH:
        return False, f"驳回原因不能超过 {REJECT_REASON_MAX_LENGTH} 字"

    activity.status = ActivityStatus.REJECTED
    activity.reject_reason = reason
    _log(activity, admin_id, AuditAction.REJECT, reason)
    db.session.commit()
    return True, None


def cancel_activity(activity, operator_id, reason=None):
    """取消活动。未结束前任意阶段都可以取消。"""
    if activity.status not in CANCELABLE_STATUSES:
        return False, f"当前状态（{activity.status_label}）不能取消"

    activity.status = ActivityStatus.CANCELED
    _log(activity, operator_id, AuditAction.CANCEL, reason)
    db.session.commit()
    return True, None


def open_check_in(activity):
    """开启签到。

    已发布的活动会被推进到「进行中」：签到通道打开的同时，报名与取消报名
    一起关闭，这样「名单」和「到场名单」的口径不会互相打架。

    已经有签到码就复用，不重新生成——码可能已经写在黑板上或投在屏幕上了。
    """
    if activity.status not in (ActivityStatus.PUBLISHED, ActivityStatus.ONGOING):
        return False, "只有已发布或进行中的活动才能开启签到"

    activity.status = ActivityStatus.ONGOING
    if not activity.check_in_code:
        activity.check_in_code = generate_check_in_code()
    db.session.commit()
    return True, None


# ---------------------------------------------------------------------------
# 活动表单校验
# ---------------------------------------------------------------------------

# 表单字段（同时也是回填用的字段名）
ACTIVITY_FORM_FIELDS = (
    "title",
    "category_id",
    "cover_url",
    "description",
    "start_time",
    "end_time",
    "enroll_deadline",
    "location",
    "capacity",
)

FIELD_LABELS = {
    "title": "活动标题",
    "category_id": "活动分类",
    "cover_url": "封面地址",
    "start_time": "开始时间",
    "end_time": "结束时间",
    "enroll_deadline": "报名截止时间",
    "location": "活动地点",
    "capacity": "人数上限",
}


def collect_activity_form(form):
    """从表单里取值。

    保留用户输入的**原始字符串**，而不是当场转成数字和时间——
    校验失败时要把原样内容回填，转过了就回不去（"007" 会变成 "7"）。
    """
    return {name: (form.get(name) or "").strip() for name in ACTIVITY_FORM_FIELDS}


def validate_activity_form(raw, valid_category_ids, capacity_floor=0):
    """校验活动表单。

    返回 ``(errors, parsed)``。errors 是「字段名 -> 提示」，空字典表示通过。

    刻意**按字段返回错误**而不是一句「表单有误」：界面需要把提示放在出错的
    输入框旁边，并且**保留用户已经填好的其它内容**——让人从头再填一遍，
    是最容易招致放弃的体验。
    """
    errors = {}
    parsed = {}

    title = raw["title"]
    if not title:
        errors["title"] = "活动标题不能为空"
    elif len(title) > TITLE_MAX_LENGTH:
        errors["title"] = f"活动标题不能超过 {TITLE_MAX_LENGTH} 字"
    else:
        parsed["title"] = title

    category_id = raw["category_id"]
    if not category_id:
        errors["category_id"] = "请选择活动分类"
    elif not category_id.isdigit() or int(category_id) not in valid_category_ids:
        errors["category_id"] = "所选分类不存在"
    else:
        parsed["category_id"] = int(category_id)

    cover_url = raw["cover_url"]
    if len(cover_url) > COVER_URL_MAX_LENGTH:
        errors["cover_url"] = f"封面地址不能超过 {COVER_URL_MAX_LENGTH} 字"
    else:
        parsed["cover_url"] = cover_url or None

    # 简介是纯文本，不做富文本编辑器
    parsed["description"] = raw["description"] or None

    location = raw["location"]
    if not location:
        errors["location"] = "活动地点不能为空"
    elif len(location) > LOCATION_MAX_LENGTH:
        errors["location"] = f"活动地点不能超过 {LOCATION_MAX_LENGTH} 字"
    else:
        parsed["location"] = location

    capacity_raw = raw["capacity"]
    if not capacity_raw:
        errors["capacity"] = "人数上限不能为空"
    elif not capacity_raw.isdigit() or int(capacity_raw) <= 0:
        errors["capacity"] = "人数上限必须是大于 0 的整数"
    elif int(capacity_raw) < capacity_floor:
        # 上限不能让「已报名的人」凭空超出名额
        errors["capacity"] = f"人数上限不能小于当前已报名人数（{capacity_floor} 人）"
    else:
        parsed["capacity"] = int(capacity_raw)

    times = {}
    for name in ("start_time", "end_time", "enroll_deadline"):
        value = parse_local(raw[name])
        if value is None:
            errors[name] = (
                f"请选择{FIELD_LABELS[name]}" if not raw[name] else f"{FIELD_LABELS[name]}格式不正确"
            )
        else:
            times[name] = value
    parsed.update(times)

    if "end_time" in times and "start_time" in times:
        if times["end_time"] <= times["start_time"]:
            errors["end_time"] = "结束时间必须晚于开始时间"

    if "enroll_deadline" in times and "start_time" in times:
        if times["enroll_deadline"] > times["start_time"]:
            errors["enroll_deadline"] = "报名截止时间不能晚于活动开始时间"

    return errors, parsed


# ---------------------------------------------------------------------------
# 权限判定（供视图层复用，真正的拦截仍在 permissions.py 的装饰器里）
# ---------------------------------------------------------------------------


def can_edit_activity(user, activity):
    """当前用户能否编辑这个活动。"""
    if activity.status not in EDITABLE_STATUSES:
        return False
    if user.is_admin:
        return True
    return (
        user.is_club
        and activity.organizer_id == user.id
        and activity.status in CLUB_EDITABLE_STATUSES
    )


def edit_blocked_reason(user, activity):
    """不能编辑时给出**具体**原因，而不是让按钮无声无息地消失。"""
    if activity.organizer_id == user.id or user.is_admin:
        if activity.status not in EDITABLE_STATUSES:
            return f"活动处于「{activity.status_label}」，信息已锁定，不可再修改"
        if not user.is_admin and activity.status not in CLUB_EDITABLE_STATUSES:
            return "活动已发布，社团不能再修改；如需调整请联系管理员"
        return None
    return "只有活动的创建者或管理员可以修改"


# ---------------------------------------------------------------------------
# 报名：并发控制的核心
# ---------------------------------------------------------------------------


def _find_enrollment(activity_id, user_id):
    return db.session.execute(
        db.select(Enrollment).where(
            Enrollment.activity_id == activity_id,
            Enrollment.user_id == user_id,
        )
    ).scalar_one_or_none()


def enroll_blocked_reason(activity_id, now=None):
    """报名被拒时，回头查一次活动，给出具体原因。

    为什么不在扣减**之前**先判断一遍、直接返回原因？因为那就退化成
    「先查再插」，竞态窗口又回来了。这里的查询只发生在已经确定失败之后，
    纯粹是为了把提示语说清楚，不参与任何判断。
    """
    now = now or utcnow()
    activity = db.session.get(Activity, activity_id)
    if activity is None:
        return "活动不存在"
    if activity.status == ActivityStatus.CANCELED:
        return "该活动已取消"
    if activity.status == ActivityStatus.FINISHED:
        return "该活动已结束"
    if activity.status == ActivityStatus.ONGOING:
        return "活动已开始，报名通道已关闭"
    if activity.status != ActivityStatus.PUBLISHED:
        return "该活动尚未发布"
    if now >= activity.enroll_deadline:
        return "报名已截止"
    if activity.enrolled_count >= activity.capacity:
        return "名额已满"
    return "报名失败，请刷新页面后重试"


def enroll(activity_id, user_id):
    """报名。返回 ``(是否成功, 失败原因)``。

    这是整个系统最容易翻车的地方，写法上有两条硬要求：

    1. **判断与扣减压在同一条 UPDATE 里完成**。写成「先 SELECT 查人数、
       再 INSERT」一定会超卖——两步之间的空隙里，别的请求完全可以把最后
       一个名额抢走。
    2. **唯一索引兜底**。同一个人并发点两次报名，两个请求都会扣减成功，
       但只会有一条报名记录能插进去，另一个撞唯一索引后退回。
    """
    now = utcnow()

    existing = _find_enrollment(activity_id, user_id)
    if existing is not None and existing.status == EnrollStatus.ENROLLED:
        # 这只是为了给出更准确的提示语，真正的防重仍然靠唯一索引
        return False, "你已经报名过这个活动了"

    result = db.session.execute(
        db.update(Activity)
        .where(
            Activity.id == activity_id,
            Activity.status == ActivityStatus.PUBLISHED,
            Activity.enroll_deadline > now,
            Activity.enrolled_count < Activity.capacity,
        )
        .values(enrolled_count=Activity.enrolled_count + 1)
    )

    if result.rowcount != 1:
        db.session.rollback()
        return False, enroll_blocked_reason(activity_id, now)

    if existing is not None:
        # 之前取消过：把旧记录改回「已报名」，而不是插一条新的。
        # 唯一索引 uk_activity_user 决定了同一个人对同一个活动只能有一条记录。
        existing.status = EnrollStatus.ENROLLED
        existing.enrolled_at = now
        existing.canceled_at = None
    else:
        db.session.add(
            Enrollment(
                activity_id=activity_id,
                user_id=user_id,
                status=EnrollStatus.ENROLLED,
                enrolled_at=now,
            )
        )

    try:
        db.session.commit()
    except IntegrityError:
        # 并发双提交时必然有一个走到这里。回滚会把上面那句 UPDATE 的扣减
        # 一并撤销，名额不会被白扣。
        db.session.rollback()
        return False, "你已经报名过这个活动了"

    return True, None


def cancel_blocked_reason(activity, enrollment, now=None):
    """不能取消报名时的具体原因；可以取消返回 ``None``。"""
    now = now or utcnow()

    if activity.status == ActivityStatus.CANCELED:
        return "活动已取消"
    if activity.status == ActivityStatus.FINISHED:
        return "活动已结束"
    if activity.status == ActivityStatus.ONGOING:
        return "活动已开始，无法取消报名"
    if enrollment.has_checked_in:
        # 已签到还允许取消，统计口径会前后矛盾
        return "你已签到，无法取消报名"
    if now >= activity.enroll_deadline:
        return "报名已截止，名单已锁定"
    if activity.enroll_deadline - now < CANCEL_LOCK_WINDOW:
        return CANCEL_LOCK_MESSAGE
    if activity.status != ActivityStatus.PUBLISHED:
        return "该活动尚未发布"
    return None


def cancel_deadline_text(activity):
    """还能取消到什么时候；始终可取消返回 ``None``。

    界面要求：**不只是在锁定后把按钮置灰，在还能取消的时候就要把截止时刻
    写出来**，让用户知道还剩多久可以反悔。所以锁定窗口的起点要能算出来。
    """
    return activity.enroll_deadline - CANCEL_LOCK_WINDOW


def cancel_enrollment(activity_id, user_id):
    """取消报名。返回 ``(是否成功, 失败原因)``。"""
    now = utcnow()

    activity = db.session.get(Activity, activity_id)
    if activity is None:
        return False, "活动不存在"

    enrollment = _find_enrollment(activity_id, user_id)
    if enrollment is None or enrollment.status != EnrollStatus.ENROLLED:
        return False, "你还没有报名这个活动"

    reason = cancel_blocked_reason(activity, enrollment, now)
    if reason:
        return False, reason

    # 同样靠「影响行数」判断，避免两个标签页同时点取消把计数扣两次
    result = db.session.execute(
        db.update(Enrollment)
        .where(
            Enrollment.id == enrollment.id,
            Enrollment.status == EnrollStatus.ENROLLED,
        )
        .values(status=EnrollStatus.CANCELED, canceled_at=now)
    )
    if result.rowcount != 1:
        db.session.rollback()
        return False, "取消失败，请刷新页面后重试"

    # 计数回退。带上 enrolled_count > 0 是为了挡住计数为负这种不可能但很脏的数据
    db.session.execute(
        db.update(Activity)
        .where(Activity.id == activity_id, Activity.enrolled_count > 0)
        .values(enrolled_count=Activity.enrolled_count - 1)
    )
    db.session.commit()
    return True, None


# ---------------------------------------------------------------------------
# 签到
# ---------------------------------------------------------------------------


def check_in_by_code(activity_id, user_id, code):
    """学生凭 6 位签到码自助签到。"""
    activity = db.session.get(Activity, activity_id)
    if activity is None:
        return False, "活动不存在"
    if activity.status != ActivityStatus.ONGOING:
        return False, "活动未开始或已结束，签到通道已关闭"
    if not activity.check_in_code:
        return False, "本次活动的签到尚未开启"
    if (code or "").strip() != activity.check_in_code:
        return False, "签到码不正确"

    enrollment = _find_enrollment(activity_id, user_id)
    if enrollment is None or enrollment.status != EnrollStatus.ENROLLED:
        return False, "你没有报名这个活动，无法签到"
    if enrollment.has_checked_in:
        return False, "你已经签到过了"

    enrollment.check_in_at = utcnow()
    # 自助签到没有核验人
    enrollment.check_in_by = None
    db.session.commit()
    return True, None


def set_check_in(activity_id, target_user_id, operator_id, checked_in):
    """组织者在名单上手动勾选 / 撤销签到。"""
    activity = db.session.get(Activity, activity_id)
    if activity is None:
        return False, "活动不存在"
    if activity.status != ActivityStatus.ONGOING:
        return False, "只有进行中的活动才能核验签到"

    enrollment = _find_enrollment(activity_id, target_user_id)
    if enrollment is None or enrollment.status != EnrollStatus.ENROLLED:
        return False, "该用户没有报名这个活动"

    if checked_in:
        if enrollment.has_checked_in:
            return False, "该用户已经签到了"
        enrollment.check_in_at = utcnow()
        enrollment.check_in_by = operator_id
    else:
        if not enrollment.has_checked_in:
            return False, "该用户还没有签到"
        enrollment.check_in_at = None
        enrollment.check_in_by = None

    db.session.commit()
    return True, None


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


def _rate(numerator, denominator):
    """百分比，保留一位小数。分母为 0 时返回 0。"""
    if not denominator:
        return 0.0
    return round(numerator * 100.0 / denominator, 1)


def enrolled_count(activity_id):
    """报名人数。口径：状态为「已报名」的记录数。

    「已取消」不计入；「缺席」（报名了但没签到）计入 —— 这正是
    product-design.md 第 7 节 F7 里定下的口径。
    """
    return db.session.execute(
        db.select(db.func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.activity_id == activity_id,
            Enrollment.status == EnrollStatus.ENROLLED,
        )
    ).scalar_one()


def checked_in_count(activity_id):
    """实到人数。"""
    return db.session.execute(
        db.select(db.func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.activity_id == activity_id,
            Enrollment.status == EnrollStatus.ENROLLED,
            Enrollment.check_in_at.is_not(None),
        )
    ).scalar_one()


def activity_stats(activity):
    """单场活动的核心指标。

    * **签到率** = 实到 ÷ 报名 —— 「报名的人来了多少」
    * **上座率** = 报名 ÷ 人数上限 —— 「名额用掉了多少」

    两个要一起看：签到率低而上座率高，说明宣传到位但内容不吸引人；
    两个都低，说明宣传本身有问题。
    """
    enrolled = enrolled_count(activity.id)
    checked = checked_in_count(activity.id)
    return {
        "enrolled": enrolled,
        "checked_in": checked,
        "absent": enrolled - checked,
        "capacity": activity.capacity,
        "remaining": max(activity.capacity - enrolled, 0),
        "check_in_rate": _rate(checked, enrolled),
        "fill_rate": _rate(enrolled, activity.capacity),
    }


def organizer_overview(activity_ids):
    """组织者的汇总口径：所有活动加总后再算率，不是各活动率的平均。

    平均会失真（一场 2 人活动 100% 签到，会把 200 人活动 50% 的签到率
    拉到 75%），加总才是真实到场情况。
    """
    if not activity_ids:
        return {
            "activity_count": 0,
            "enrolled": 0,
            "checked_in": 0,
            "absent": 0,
            "check_in_rate": 0.0,
        }

    row = db.session.execute(
        db.select(
            db.func.count(Enrollment.id),
            db.func.count(Enrollment.check_in_at),
        ).where(
            Enrollment.activity_id.in_(activity_ids),
            Enrollment.status == EnrollStatus.ENROLLED,
        )
    ).one()
    enrolled = row[0] or 0
    checked = row[1] or 0

    return {
        "activity_count": len(activity_ids),
        "enrolled": enrolled,
        "checked_in": checked,
        "absent": enrolled - checked,
        "check_in_rate": _rate(checked, enrolled),
    }


def check_in_state(activity, enrollment, now=None):
    """学生对某条报名记录的签到状态：待参加 / 待签到 / 已签到 / 缺席。

    这四种状态是产品文档 F6 明确要求区分开的。
    """
    now = now or utcnow()
    if enrollment is None:
        return None
    if enrollment.has_checked_in:
        return "checked_in"
    if activity.status == ActivityStatus.ONGOING:
        return "awaiting_check_in"
    if activity.status == ActivityStatus.FINISHED:
        return "absent"
    if activity.status == ActivityStatus.CANCELED:
        return "canceled"
    if activity.status == ActivityStatus.PUBLISHED:
        return "upcoming"
    return "upcoming"


CHECK_IN_STATE_LABELS = {
    "checked_in": "已签到",
    "awaiting_check_in": "待签到",
    "absent": "缺席",
    "canceled": "已取消",
    "upcoming": "待参加",
}

# Bootstrap 徽章配色
CHECK_IN_STATE_STYLES = {
    "checked_in": "text-bg-success",
    "awaiting_check_in": "text-bg-warning",
    "absent": "text-bg-danger",
    "canceled": "text-bg-secondary",
    "upcoming": "text-bg-light border",
}


def reconciliation_report():
    """对账：`enrolled_count` 冗余计数与真实报名记录数不一致的活动。

    这个计数是并发控制的载体，一旦因为异常路径被写歪，光看表面数字看不出来。
    留一条校验口子，排查时有据可查。
    """
    rows = db.session.execute(
        db.select(Activity, db.func.count(Enrollment.id))
        .outerjoin(
            Enrollment,
            db.and_(
                Enrollment.activity_id == Activity.id,
                Enrollment.status == EnrollStatus.ENROLLED,
            ),
        )
        .group_by(Activity.id)
    ).all()

    return [
        {"activity": activity, "actual": actual, "recorded": activity.enrolled_count}
        for activity, actual in rows
        if actual != activity.enrolled_count
    ]
