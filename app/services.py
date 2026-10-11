"""业务规则层（V2.0）。

把规则集中在这里、而不是散落在各个视图里，有三个理由：

1. **同一条规则会被多个入口用到**。比如「能不能报名」既要在详情页决定按钮
   状态，也要在真正提交报名时再判一次。写成两份，迟早会不一致。
2. **并发控制必须是原子的**。扣名额的写法一旦被复制到别处，很容易被写成
   「先查人数、再插入」——那正是这类系统最经典的超卖缺陷。
3. **测试可以直接测规则本身**，不必每次都绕经 HTTP 请求。

V2.0 的核心是三件事（出处：《实验2-需求分析.md》）：

* **候补机制**（FR-4）——满员不再把报名丢掉：进入候补队列、按时间先到先得、
  退出不留空档、名额空出时队首自动递补；
* **报名审核**（FR-6）——需审核的活动由组织教师逐人确认，
  **通过 ≠ 正式**：有余位才转正式，没余位进候补；
* **容量硬约束**（BR-1）——任何操作之后，「正式人数 ≤ 上限」都必须成立，
  包括两个人同时抢最后一个名额的时刻。

报名的五态流转见 `models.EnrollStatus`；状态机图见需求分析第七章。
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

# 取消「正式」报名的锁定窗口。距报名截止不足这个时长起，名单锁定、不可再取消。
# 取 24 小时是「给组织者留出按名单备料的时间」与「给学生留出反悔空间」的折中。
#
# ⚠️ 只作用于「正式」报名。「候补退出」「待审核撤销」随时可用（FR-4.4：
# 候补者可随时退出）——那两种状态不占名额，退出没有成本，不该被锁住。
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
QUALIFICATION_MAX_LENGTH = 255

# 允许「编辑」的状态。
#
# V2.0 明确允许组织教师修改本人已经发布的活动（FR-7.2「时间、地点等有变化时
# 可及时调整」）。但活动一旦开始（进行中）就整体锁定：报名名单、签到都以
# 当时的信息为准，边办边改活动信息只会让人跑空场地。
EDITABLE_STATUSES = (
    ActivityStatus.DRAFT,
    ActivityStatus.PUBLISHED,
)

# 允许取消活动的状态：未结束前任意阶段 ──> 已取消。
# 被管理员下线的活动不能由教师自行取消（先恢复再说）。
CANCELABLE_STATUSES = (
    ActivityStatus.DRAFT,
    ActivityStatus.PUBLISHED,
    ActivityStatus.ONGOING,
)

REASON_MAX_LENGTH = 255


# ---------------------------------------------------------------------------
# 活动状态流转
# ---------------------------------------------------------------------------


def generate_check_in_code():
    """生成 6 位数字签到码。

    用 ``secrets`` 而不是 ``random``：签到码决定谁能签到，
    ``random`` 基于可预测的伪随机序列，知道几个输出就能推出后续输出。
    """
    return "".join(secrets.choice("0123456789") for _ in range(CHECK_IN_CODE_LENGTH))


def sync_activity_statuses(now=None):
    """按时间把活动推进到下一个状态。

    ``PUBLISHED`` 到点变 ``ONGOING``，``ONGOING`` 到点变 ``FINISHED``。

    被管理员下线的活动（``OFFLINE``）不参与推进：它已经不在时间线上，
    恢复时再由 `restore_activity` 按时间重新定位。

    **为什么用「每次请求顺手同步」而不是定时任务**：本系统只跑一个进程、
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
    """记一条活动流转日志。这张表只追加、不修改。"""
    db.session.add(
        AuditLog(
            activity_id=activity.id,
            operator_id=operator_id,
            action=action,
            reason=(reason or None) and reason[:REASON_MAX_LENGTH],
        )
    )


def latest_offline_records(activity_ids):
    """批量取这些活动各自最近一次的「下线」记录（一次查询，避免 N+1）。

    用于活动列表上的「处理标记」：被下线时是谁、什么时候、为什么。
    """
    if not activity_ids:
        return {}
    logs = (
        db.session.execute(
            db.select(AuditLog)
            .where(
                AuditLog.activity_id.in_(activity_ids),
                AuditLog.action == AuditAction.OFFLINE,
            )
            .order_by(AuditLog.created_at)
        )
        .scalars()
        .all()
    )
    latest = {}
    for log in logs:
        # 按时间升序读出，后者覆盖前者 —— 最后留下的就是最新一条
        latest[log.activity_id] = log
    return latest


def latest_offline_record(activity_id):
    return latest_offline_records([activity_id]).get(activity_id)


def publish_activity(activity, operator_id):
    """发布活动：草稿 → 已发布。

    V2.0 里**教师自己就是发布者**，不再有「提交给管理员审核」这一步
    （需求分析 3.1：管理员 ✗ 创建 / 发布 / 修改活动）。管理员改为事后监督：
    发现问题用「下线」处理（见 `offline_activity`）。
    """
    if activity.status != ActivityStatus.DRAFT:
        return False, f"当前状态（{activity.status_label}）不能发布"
    if activity.organizer_id != operator_id:
        return False, "只能发布本人负责的活动"

    activity.status = ActivityStatus.PUBLISHED
    _log(activity, operator_id, AuditAction.PUBLISH)
    db.session.commit()
    return True, None


def offline_activity(activity, admin_id, reason=None):
    """管理员下线违规或明显错误的活动（A-07、DEC-5）。

    下线后：学生端不再展示、不能报名；教师端仍可见，并带「处理标记」
    （谁、什么时候、因为什么）。**不删数据、不代教师改内容、不动报名记录**
    —— 这是管理员在活动上的全部权力边界。
    """
    if activity.status not in ActivityStatus.OFFINABLE:
        return False, f"当前状态（{activity.status_label}）不能下线"

    activity.status = ActivityStatus.OFFLINE
    _log(activity, admin_id, AuditAction.OFFLINE, reason)
    db.session.commit()
    return True, None


def restore_activity(activity, admin_id, reason=None):
    """恢复被下线的活动。

    恢复后先回到「已发布」，再立刻按时间重新定位——被下线期间活动可能早已
    开始甚至结束，不能让它挂着「已发布」的牌子等着别人来报名。
    """
    if activity.status != ActivityStatus.OFFLINE:
        return False, "只有已下线的活动可以恢复"

    activity.status = ActivityStatus.PUBLISHED
    _log(activity, admin_id, AuditAction.RESTORE, reason)
    db.session.commit()
    sync_activity_statuses()
    return True, None


def cancel_activity(activity, operator_id, reason=None):
    """取消活动。未结束前任意阶段都可以取消（状态机：未结束前 ⟶ 已取消）。"""
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
    "qualification",
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
    "qualification": "参加资格说明",
}


def collect_activity_form(form):
    """从表单里取值。

    保留用户输入的**原始字符串**，而不是当场转成数字和时间——
    校验失败时要把原样内容回填，转过了就回不去（"007" 会变成 "7"）。
    """
    raw = {name: (form.get(name) or "").strip() for name in ACTIVITY_FORM_FIELDS}
    # 复选框：勾上时浏览器提交 "on"（或自定义的 "1"），不勾则整个字段都不出现
    raw["requires_review"] = "1" if form.get("requires_review") else ""
    return raw


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

    # ---- 报名审核开关与参加资格（FR-6.1）----
    requires_review = bool(raw.get("requires_review"))
    qualification = (raw.get("qualification") or "").strip()
    if len(qualification) > QUALIFICATION_MAX_LENGTH:
        errors["qualification"] = f"参加资格说明不能超过 {QUALIFICATION_MAX_LENGTH} 字"
    elif requires_review and not qualification:
        # 需审核的活动，"审"的就是资格——资格说明空着，教师审起来没有依据，
        # 学生也不知道自己该不该报。
        errors["qualification"] = "勾选「需要审核」时，请填写参加资格说明"
    else:
        parsed["qualification"] = qualification or None
    parsed["requires_review"] = requires_review

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
    """当前用户能否编辑这个活动。

    V2.0 的边界（3.1 权限矩阵）：**只有该活动的组织教师本人**可以编辑；
    管理员不代教师修改内容（他对活动的工具是「下线 / 恢复」）。
    """
    if not user.is_teacher:
        return False
    return activity.organizer_id == user.id and activity.status in EDITABLE_STATUSES


def edit_blocked_reason(user, activity):
    """不能编辑时给出**具体**原因，而不是让按钮无声无息地消失。"""
    if not user.is_teacher:
        return "只有活动组织教师可以编辑活动；管理员不代教师修改内容"
    if activity.organizer_id != user.id:
        return "只能修改本人负责的活动"
    if activity.status not in EDITABLE_STATUSES:
        return f"活动处于「{activity.status_label}」，信息已锁定，不可再修改"
    return None


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


def _take_slot(activity_id, now, check_deadline=True):
    """构造「原子占位」语句：把正式人数 +1，前提是当时确实还有余位。

    判断与扣减压在**同一条 UPDATE** 里完成，这是防超卖的关键——
    写成「先 SELECT 查人数、再 INSERT」一定会在两步之间被抢走最后一个名额。

    :param check_deadline: 报名走 True（截止后通道关闭）；
        递补走 False——名额空出就该补上，截止时间卡的是「新报名」，
        不是「把已经在队列里的人补进来」。
    """
    conditions = [
        Activity.id == activity_id,
        Activity.status == ActivityStatus.PUBLISHED,
        Activity.enrolled_count < Activity.capacity,
    ]
    if check_deadline:
        conditions.append(Activity.enroll_deadline > now)
    return (
        db.update(Activity)
        .where(*conditions)
        .values(enrolled_count=Activity.enrolled_count + 1)
    )


def _release_slot(activity_id):
    """构造「释放名额」语句。带 ``enrolled_count > 0`` 护栏挡住计数为负。"""
    return (
        db.update(Activity)
        .where(Activity.id == activity_id, Activity.enrolled_count > 0)
        .values(enrolled_count=Activity.enrolled_count - 1)
    )


def enroll_allowed_reason(activity, now=None):
    """报名通道是否开放；不可报名时返回原因，可以则返回 ``None``。

    ⚠️ **满员不算「不可报名」**：V2.0 里满员时报名会进入候补队列（BR-3），
    是一种成功，不是失败。这里只拦「通道关闭」的情形。
    """
    now = now or utcnow()
    if activity.status == ActivityStatus.OFFLINE:
        return "该活动已被管理员下线，暂不接受报名"
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
    return None


def enroll_blocked_reason(activity_id, now=None):
    """报名被拒时，回头查一次活动，给出具体原因。

    为什么不在扣减**之前**先判断一遍、直接返回原因？因为判断和扣减之间
    存在竞态窗口——本函数只发生在已经确定失败之后，纯粹是把提示语说清楚。
    """
    activity = db.session.get(Activity, activity_id)
    if activity is None:
        return "活动不存在"
    return enroll_allowed_reason(activity, now)


def _write_application(existing, activity_id, user_id, status, now):
    """写入 / 复用一条报名记录，并返回候补排位（非候补时为 None）。

    * 「复用」：唯一索引 ``uk_activity_user`` 决定了同一个人对同一个活动
      只能有一条记录。取消 / 被拒后重新报名时改写旧记录，而不是插新的。
    * 时间线整体重置为「这一轮报名」：重新报名按**新的**时间重新排队
      （TC-6、DEC-3），上一轮的审核、签到痕迹一并清掉。
    """
    if existing is None:
        existing = Enrollment(activity_id=activity_id, user_id=user_id)
        db.session.add(existing)

    existing.status = status
    existing.enrolled_at = now
    existing.canceled_at = None
    existing.reviewed_at = None
    existing.reviewed_by = None
    existing.check_in_at = None
    existing.check_in_by = None
    existing.waitlisted_at = now if status == EnrollStatus.WAITLISTED else None

    db.session.flush()  # 让紧接着的排位查询能看到这一行
    if status == EnrollStatus.WAITLISTED:
        return waitlist_position(existing)
    return None


def enroll(activity_id, user_id):
    """报名。返回 ``(是否成功, 说明)``——成功时说明里直接给结果文案。

    分流规则（FR-2）：

    * **需审核** → 待审核（不占名额），等教师逐人确认；
    * **无需审核 + 有余位** → 正式（原子占位）；
    * **无需审核 + 满员** → 候补（记录进入候补的时间，排队等位）。

    并发上有两条硬要求，写法不许退化：

    1. 判断与扣减压在同一条 UPDATE 里（见 `_take_slot`）；
    2. 唯一索引兜底——同一个人并发点两次，只会有一条记录能写进去。
    """
    now = utcnow()

    existing = _find_enrollment(activity_id, user_id)
    if existing is not None and existing.is_active:
        # 这只是为了给出更准确的提示语，真正的防重仍然靠唯一索引
        return False, f"你已经报名过这个活动了（当前状态：{existing.status_label}）"

    activity = db.session.get(Activity, activity_id)
    if activity is None:
        return False, "活动不存在"

    blocked = enroll_allowed_reason(activity, now)
    if blocked:
        return False, blocked

    if activity.requires_review:
        # 需要审核：先入「待审核」。占不占名额，由教师审核那一刻的余位决定。
        try:
            _write_application(existing, activity_id, user_id, EnrollStatus.PENDING, now)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return False, "你已经报名过这个活动了"
        return True, "报名申请已提交，等待组织教师审核确认。"

    # ---- 无需审核：先抢名额，抢不到就是候补 ----
    if db.session.execute(_take_slot(activity_id, now)).rowcount == 1:
        try:
            _write_application(
                existing, activity_id, user_id, EnrollStatus.CONFIRMED, now
            )
            db.session.commit()
        except IntegrityError:
            # 并发双提交时必然有一个走到这里。回滚会把上面那句 UPDATE 的扣减
            # 一并撤销，名额不会被白扣。
            db.session.rollback()
            return False, "你已经报名过这个活动了"
        return True, "报名成功，名额已为你保留。"

    # 占位失败：要么满员，要么通道刚刚关闭。回滚掉没生效的 UPDATE，再问清原因。
    db.session.rollback()
    blocked = enroll_blocked_reason(activity_id, now)
    if blocked:
        return False, blocked

    # 只剩一种可能：满员 → 进入候补队列（BR-3 / DEC-4）
    try:
        position = _write_application(
            existing, activity_id, user_id, EnrollStatus.WAITLISTED, now
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return False, "你已经报名过这个活动了"
    return True, f"名额已满，已进入候补队列（当前第 {position} 位）。"


def waitlist_query(activity_id):
    """某个活动的候补队列，**严格按进入候补的时间**排序（BR-4）。

    时间相同时用 id 兜底：两台机器在同一微秒写入也能得到确定顺序，
    否则「第 N 位」会随着查询而变。系统不提供任何人工调整顺序的入口。
    """
    return (
        db.select(Enrollment)
        .where(
            Enrollment.activity_id == activity_id,
            Enrollment.status == EnrollStatus.WAITLISTED,
        )
        .order_by(Enrollment.waitlisted_at, Enrollment.id)
    )


def waitlist_position(enrollment):
    """候补排位（第 N 位，从 1 开始）。不是候补则返回 ``None``。

    排位不落库，每次按时间现算——所以「候补者退出后，其后人员自动前移」
    （BR-7）是天然成立的，不需要（也没有）任何调整顺序的操作。
    """
    if enrollment.status != EnrollStatus.WAITLISTED or enrollment.waitlisted_at is None:
        return None
    ahead = db.session.execute(
        db.select(db.func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.activity_id == enrollment.activity_id,
            Enrollment.status == EnrollStatus.WAITLISTED,
            db.or_(
                Enrollment.waitlisted_at < enrollment.waitlisted_at,
                db.and_(
                    Enrollment.waitlisted_at == enrollment.waitlisted_at,
                    Enrollment.id < enrollment.id,
                ),
            ),
        )
    ).scalar_one()
    return ahead + 1


def waitlist_count(activity_id):
    return db.session.execute(
        db.select(db.func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.activity_id == activity_id,
            Enrollment.status == EnrollStatus.WAITLISTED,
        )
    ).scalar_one()


def pending_count(activity_id):
    return db.session.execute(
        db.select(db.func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.activity_id == activity_id,
            Enrollment.status == EnrollStatus.PENDING,
        )
    ).scalar_one()


def promote_waitlist(activity_id, now=None):
    """自动递补（FR-4.5 / BR-6）：名额空出时，队首自动转为正式。

    **在哪里调用**：任何可能让「余位 > 0」的操作之后——正式报名取消、
    教师把上限调大。一次调用会把队列一直消化到「又满了」或「没人等」为止，
    调用方不需要（也不应该）自己判断该补几个。

    并发安全：递补占位仍然走那条原子的条件 UPDATE，两个人同时触发也只会
    有一个成功；另一个发现没有余位就停下。队列顺序由 `waitlist_query`
    保证，**任何角色都不能指定补谁**。

    ⚠️ **本函数不提交事务**——它是"名额空出"那个操作的一部分，要和调用方的
    改名/释放放在同一个事务里，递补与空位之间才没有可被抢走的窗口。
    调用方负责 ``db.session.commit()``。
    """
    now = now or utcnow()
    promoted = []
    while True:
        head = (
            db.session.execute(waitlist_query(activity_id).limit(1))
            .scalars()
            .first()
        )
        if head is None:
            break
        if db.session.execute(_take_slot(activity_id, now, check_deadline=False)).rowcount != 1:
            break  # 没余位了（或活动已不在可报状态），停
        head.status = EnrollStatus.CONFIRMED
        # waitlisted_at 保留：它记录的是「曾经什么时候排进队列」的历史，
        # 状态已是正式，不会再出现在候补队列的任何查询里。
        promoted.append(head.user_id)
    return promoted


def review_enrollment(activity_id, user_id, reviewer_id, approve):
    """教师审核一条「待审核」报名。返回 ``(是否成功, 说明)``。

    结果衔接（FR-6.3 / BR-9）——这是 V2.0 最容易被写错的一处：

    * **拒绝** → 已拒绝，本次不能参加，也**不得进入候补**；
    * **通过** → 「通过」的是资格，不是名额：当时有余位才转正式；
      没余位就排进候补队列，队列记录**通过的时刻**（TC-8）。

    并发上和报名同源：转正式那一步仍然走原子占位，抢不到就落到候补，
    不存在"审核通过把名额顶爆"的路径。
    """
    now = utcnow()

    activity = db.session.get(Activity, activity_id)
    if activity is None:
        return False, "活动不存在"

    enrollment = _find_enrollment(activity_id, user_id)
    if enrollment is None or enrollment.status != EnrollStatus.PENDING:
        return False, "这条申请已经处理过了"

    if not approve:
        enrollment.status = EnrollStatus.REJECTED
        enrollment.reviewed_at = now
        enrollment.reviewed_by = reviewer_id
        db.session.commit()
        return True, "已拒绝该申请。"

    # 占用余位时不看报名截止：学生是在截止前提交的申请，教师审核往往发生在
    # 截止之后——这时候"有没有余位"只该看有没有空位，不该再被截止时间拦一道。
    if db.session.execute(
        _take_slot(activity_id, now, check_deadline=False)
    ).rowcount == 1:
        enrollment.status = EnrollStatus.CONFIRMED
        message = "已通过，该学生转为正式。"
    else:
        enrollment.status = EnrollStatus.WAITLISTED
        enrollment.waitlisted_at = now  # 队列按「通过时刻」排序（TC-8）
        db.session.flush()
        message = f"已通过，但当前没有余位，该学生排在第 {waitlist_position(enrollment)} 位候补。"

    enrollment.reviewed_at = now
    enrollment.reviewed_by = reviewer_id
    db.session.commit()
    return True, message


def cancel_blocked_reason(activity, enrollment, now=None):
    """不能取消 / 退出 / 撤销时的具体原因；可以则返回 ``None``。

    V2.0 的取舍得说清楚：

    * 「正式」受名单锁定保护——报名截止后不可取消，临近截止（24 小时内）
      也不可取消。正式取消是**唯一会释放名额**的操作，必须受控。
    * 「候补」「待审核」不占名额，退出没有成本：**随时可退 / 可撤**
      （FR-4.4 明确写了候补者"随时退出"）。活动整体结束后统一冻结。
    """
    now = now or utcnow()

    if activity.status == ActivityStatus.OFFLINE:
        return "该活动已被管理员下线，暂不能操作"
    if activity.status == ActivityStatus.CANCELED:
        return "活动已取消"
    if activity.status == ActivityStatus.FINISHED:
        return "活动已结束"

    if enrollment.status in (EnrollStatus.PENDING, EnrollStatus.WAITLISTED):
        return None

    if activity.status == ActivityStatus.ONGOING:
        return "活动已开始，名单已锁定"
    if activity.status != ActivityStatus.PUBLISHED:
        return "该活动尚未发布"
    if enrollment.has_checked_in:
        # 已签到还允许取消，统计口径会前后矛盾
        return "你已签到，无法取消报名"
    if now >= activity.enroll_deadline:
        return "报名已截止，名单已锁定"
    if activity.enroll_deadline - now < CANCEL_LOCK_WINDOW:
        return CANCEL_LOCK_MESSAGE
    return None


def cancel_deadline_text(activity):
    """还能取消到什么时候；始终可取消返回 ``None``。

    界面要求：**不只是在锁定后把按钮置灰，在还能取消的时候就要把截止时刻
    写出来**，让用户知道还剩多久可以反悔。所以锁定窗口的起点要能算出来。
    """
    return activity.enroll_deadline - CANCEL_LOCK_WINDOW


def cancel_enrollment(activity_id, user_id):
    """取消报名 / 撤销申请 / 退出候补。返回 ``(是否成功, 失败原因)``。

    正式取消会**释放名额并立即触发递补**（FR-5.1 / BR-5 / BR-6）；
    候补退出只是把自己移出队列，后面的排位天然前移（BR-7）。
    """
    now = utcnow()

    activity = db.session.get(Activity, activity_id)
    if activity is None:
        return False, "活动不存在"

    enrollment = _find_enrollment(activity_id, user_id)
    if enrollment is None or not enrollment.is_active:
        return False, "你还没有报名这个活动"

    reason = cancel_blocked_reason(activity, enrollment, now)
    if reason:
        return False, reason

    was_confirmed = enrollment.status == EnrollStatus.CONFIRMED
    current_status = enrollment.status

    # 同样靠「影响行数」判断，避免两个标签页同时点取消把状态改两次
    result = db.session.execute(
        db.update(Enrollment)
        .where(
            Enrollment.id == enrollment.id,
            Enrollment.status == current_status,
        )
        .values(
            status=EnrollStatus.CANCELED,
            canceled_at=now,
            # 退出候补后把 waitlisted_at 清掉：这条记录已经不在队列里，
            # 保留着只会让"曾经排过队"和"现在在排队"看起来一样。
            waitlisted_at=None,
        )
    )
    if result.rowcount != 1:
        db.session.rollback()
        return False, "取消失败，请刷新页面后重试"

    if was_confirmed:
        db.session.execute(_release_slot(activity_id))
        # 名额空出 → 队首立即转正（同一事务内完成，不存在"空窗期"）
        promote_waitlist(activity_id, now)

    db.session.commit()
    return True, None


# ---------------------------------------------------------------------------
# 签到
# ---------------------------------------------------------------------------


def check_in_by_code(activity_id, user_id, code):
    """学生凭 6 位签到码自助签到。只有「正式」名单里的人能签到。"""
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
    if enrollment is None or not enrollment.is_active:
        return False, "你没有报名这个活动，无法签到"
    if enrollment.status != EnrollStatus.CONFIRMED:
        return False, f"你还不是正式名额（当前：{enrollment.status_label}），无法签到"
    if enrollment.has_checked_in:
        return False, "你已经签到过了"

    enrollment.check_in_at = utcnow()
    # 自助签到没有核验人
    enrollment.check_in_by = None
    db.session.commit()
    return True, None


def set_check_in(activity_id, target_user_id, operator_id, checked_in):
    """组织者在名单上手动勾选 / 撤销签到。只对「正式」名单生效。"""
    activity = db.session.get(Activity, activity_id)
    if activity is None:
        return False, "活动不存在"
    if activity.status != ActivityStatus.ONGOING:
        return False, "只有进行中的活动才能核验签到"

    enrollment = _find_enrollment(activity_id, target_user_id)
    if enrollment is None or enrollment.status != EnrollStatus.CONFIRMED:
        return False, "该用户不在正式名单中"

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


def confirmed_count(activity_id):
    """正式人数。口径：状态为「正式」的报名记录数。

    「候补」「待审核」不计入；「缺席」（正式但没签到）计入 —— 这正是
    V1.0 沿用下来的口径，也是 `activity.enrolled_count` 的数据库口径。
    """
    return db.session.execute(
        db.select(db.func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.activity_id == activity_id,
            Enrollment.status == EnrollStatus.CONFIRMED,
        )
    ).scalar_one()


def checked_in_count(activity_id):
    """实到人数。"""
    return db.session.execute(
        db.select(db.func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.activity_id == activity_id,
            Enrollment.status == EnrollStatus.CONFIRMED,
            Enrollment.check_in_at.is_not(None),
        )
    ).scalar_one()


def activity_stats(activity):
    """单场活动的核心指标。

    * **签到率** = 实到 ÷ 正式 —— 「报名的人来了多少」
    * **上座率** = 正式 ÷ 人数上限 —— 「名额用掉了多少」
    * **候补 / 待审核** —— 队列里还压着多少人（V2.0 新增的观察口径）

    两个率要一起看：签到率低而上座率高，说明宣传到位但内容不吸引人；
    两个都低，说明宣传本身有问题。
    """
    confirmed = confirmed_count(activity.id)
    checked = checked_in_count(activity.id)
    return {
        "enrolled": confirmed,
        "checked_in": checked,
        "absent": confirmed - checked,
        "capacity": activity.capacity,
        "remaining": max(activity.capacity - confirmed, 0),
        "waitlisted": waitlist_count(activity.id),
        "pending": pending_count(activity.id),
        "check_in_rate": _rate(checked, confirmed),
        "fill_rate": _rate(confirmed, activity.capacity),
    }


def organizer_overview(activity_ids):
    """组织者的汇总口径：所有活动加总后再算率，不是各活动率的平均。

    平均会失真（一场 2 人活动 100% 签到，会把 200 人活动 50% 的签到率
    拉到 75%），加总才是真实到场情况。
    """
    empty = {
        "activity_count": 0,
        "enrolled": 0,
        "checked_in": 0,
        "absent": 0,
        "waitlisted": 0,
        "pending": 0,
        "check_in_rate": 0.0,
    }
    if not activity_ids:
        return empty

    row = db.session.execute(
        db.select(
            db.func.count(Enrollment.id),
            db.func.count(Enrollment.check_in_at),
        ).where(
            Enrollment.activity_id.in_(activity_ids),
            Enrollment.status == EnrollStatus.CONFIRMED,
        )
    ).one()
    enrolled = row[0] or 0
    checked = row[1] or 0

    waitlisted = db.session.execute(
        db.select(db.func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.activity_id.in_(activity_ids),
            Enrollment.status == EnrollStatus.WAITLISTED,
        )
    ).scalar_one()

    pending = db.session.execute(
        db.select(db.func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.activity_id.in_(activity_ids),
            Enrollment.status == EnrollStatus.PENDING,
        )
    ).scalar_one()

    return {
        "activity_count": len(activity_ids),
        "enrolled": enrolled,
        "checked_in": checked,
        "absent": enrolled - checked,
        "waitlisted": waitlisted,
        "pending": pending,
        "check_in_rate": _rate(checked, enrolled),
    }


def check_in_state(activity, enrollment, now=None):
    """学生对某条报名记录的签到状态：待参加 / 待签到 / 已签到 / 缺席。

    这四种状态是产品文档 F6 明确要求区分开的。只对「正式」报名有意义。
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

# 报名五态的徽章配色
ENROLL_STATUS_STYLES = {
    EnrollStatus.PENDING: "text-bg-info",
    EnrollStatus.CONFIRMED: "text-bg-success",
    EnrollStatus.WAITLISTED: "text-bg-warning",
    EnrollStatus.REJECTED: "text-bg-danger",
    EnrollStatus.CANCELED: "text-bg-secondary",
}


def reconciliation_report():
    """对账：`enrolled_count` 冗余计数与真实「正式」记录数不一致的活动。

    这个计数是并发控制的载体，一旦因为异常路径被写歪，光看表面数字看不出来。
    留一条校验口子，排查时有据可查。
    """
    rows = db.session.execute(
        db.select(Activity, db.func.count(Enrollment.id))
        .outerjoin(
            Enrollment,
            db.and_(
                Enrollment.activity_id == Activity.id,
                Enrollment.status == EnrollStatus.CONFIRMED,
            ),
        )
        .group_by(Activity.id)
    ).all()

    return [
        {"activity": activity, "actual": actual, "recorded": activity.enrolled_count}
        for activity, actual in rows
        if actual != activity.enrolled_count
    ]
