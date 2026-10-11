"""演示数据播种。

用途：把数据库初始化成一份**能直接走一遍完整闭环**的数据，省去手工建账号、
建分类、造报名记录的功夫。命令行用法::

    .venv\\Scripts\\python.exe -m flask init-db
    .venv\\Scripts\\python.exe -m flask seed-demo

这里刻意覆盖了全部七种活动状态，以及「名额已满」「报名已截止」两种拦截场景——
它们平时很难自然出现，却是最需要人工确认的界面分支。
"""

from datetime import timedelta

from .extensions import db
from .models import (
    Activity,
    ActivityStatus,
    AuditAction,
    AuditLog,
    Category,
    Enrollment,
    EnrollStatus,
    Role,
    User,
    utcnow,
)

# 演示账号统一用这个密码。长度满足密码策略（至少 8 位）。
DEMO_PASSWORD = "Campus-Demo-2026"

# 演示账号
DEMO_ACCOUNTS = [
    ("admin", "陈老师", Role.ADMIN, None),
    ("club001", "小张", Role.CLUB, "摄影协会"),
    ("club002", "小林", Role.CLUB, "天文社"),
    ("20230101", "李明", Role.STUDENT, None),
    ("20230102", "王芳", Role.STUDENT, None),
    ("20230103", "赵启", Role.STUDENT, None),
]

# 用来把报名名单填得像真的
EXTRA_STUDENTS = [
    ("20230104", "孙悦"),
    ("20230105", "周航"),
    ("20230106", "吴桐"),
    ("20230107", "郑楠"),
    ("20230108", "冯磊"),
    ("20230109", "何雨"),
    ("20230110", "许诺"),
    ("20230111", "蒋洲"),
    ("20230112", "沈溪"),
    ("20230113", "韩沐"),
    ("20230114", "杨帆"),
    ("20230115", "朱迪"),
]

CATEGORIES = [
    ("讲座", 1),
    ("文体", 2),
    ("志愿", 3),
    ("竞赛", 4),
]

# 播种出来的活动数量。写死成一个常量，是为了让「数据条数」这件事
# 只有一处定义——测试会拿它做断言，改数据时不会两边对不上。
ACTIVITY_COUNT = 11


def seed_demo_data():
    """写入演示数据。已有数据时抛错，避免把真实数据盖掉。"""
    if db.session.execute(db.select(db.func.count()).select_from(User)).scalar_one():
        raise RuntimeError(
            "数据库里已经有用户了。演示数据只用于初始化空库，"
            "不会覆盖已有数据；确实要重播，请加 --reset。"
        )

    now = utcnow()

    # ---- 分类 ----
    categories = {}
    for name, order in CATEGORIES:
        category = Category(name=name, sort_order=order)
        db.session.add(category)
        categories[name] = category
    db.session.flush()

    # ---- 账号 ----
    people = {}
    for student_no, name, role, club_name in DEMO_ACCOUNTS:
        person = User(
            student_no=student_no,
            name=name,
            role=role,
            club_name=club_name,
            email=f"{student_no}@campus.example.edu",
        )
        person.set_password(DEMO_PASSWORD)
        db.session.add(person)
        people[student_no] = person

    extra = []
    for student_no, name in EXTRA_STUDENTS:
        person = User(student_no=student_no, name=name, role=Role.STUDENT)
        person.set_password(DEMO_PASSWORD)
        db.session.add(person)
        extra.append(person)
    db.session.flush()

    club = people["club001"]  # 摄影协会
    other_club = people["club002"]  # 天文社
    admin = people["admin"]
    all_students = [people["20230101"], people["20230102"], people["20230103"], *extra]

    def add_audit_trail(activity):
        """按活动的**最终状态**倒推出「本来会留下」的审核日志。

        为什么需要这个：演示数据是直接把活动建成「待审核」「已驳回」这些状态的，
        绕过了真实的提交/审核流程，于是 AuditLog 一条都没有。表现是审核中心里
        「提交时间」全是「—」、审核日志写着「还没有任何审核记录」——明明有一场
        已驳回的活动。看着像审核功能没做，其实只是演示数据不完整。

        轨迹规则（和真实流程一致）：
        * 草稿：没有任何流转，不写；
        * 管理员自己创建的活动：没有「提交审核」这一步（直接发布），不写提交；
        * 其余：社团提交 → 管理员通过 / 驳回；已取消的再加一条取消。

        时间刻意**排在当前时刻之前**：审核日志里出现未来时间会让人以为时钟错了。
        """
        def log(action, operator_id, reason=None, at=None):
            db.session.add(
                AuditLog(
                    activity_id=activity.id,
                    operator_id=operator_id,
                    action=action,
                    reason=reason,
                    created_at=at or activity.created_at,
                )
            )

        status = activity.status
        if status == ActivityStatus.DRAFT:
            return

        # approved_at 在种子里只对「已发布」的活动设了，用它当审核时间更真实
        decided_at = activity.approved_at or (activity.created_at - timedelta(days=1))
        submitted_at = decided_at - timedelta(days=2)

        by_admin = activity.organizer_id == admin.id
        if not by_admin:
            log(AuditAction.SUBMIT, activity.organizer_id, at=submitted_at)

        if status == ActivityStatus.PENDING:
            return

        if status == ActivityStatus.REJECTED:
            log(AuditAction.REJECT, admin.id, reason=activity.reject_reason, at=decided_at)
            return

        if not by_admin:
            log(AuditAction.APPROVE, admin.id, at=decided_at)

        if status == ActivityStatus.CANCELED:
            log(
                AuditAction.CANCEL,
                activity.organizer_id,
                reason="因天气原因取消",
                at=decided_at + timedelta(hours=12),
            )

    def add_activity(**kwargs):
        activity = Activity(**kwargs)
        db.session.add(activity)
        db.session.flush()
        add_audit_trail(activity)
        return activity

    def enroll(activity, users, checked_in=0):
        """造报名记录，并同步维护 enrolled_count。

        直接插报名表而不动计数，会造出生产里不可能出现的脏数据——
        而这个字段正是并发控制的载体，对不上会让后面所有排查都失效。

        :param checked_in: 前几个人「已签到」，用来造出有缺席的统计样本
        """
        for index, person in enumerate(users):
            is_checked = index < checked_in
            db.session.add(
                Enrollment(
                    activity_id=activity.id,
                    user_id=person.id,
                    status=EnrollStatus.ENROLLED,
                    enrolled_at=activity.enroll_deadline - timedelta(days=2),
                    check_in_at=activity.start_time + timedelta(minutes=5 + index)
                    if is_checked
                    else None,
                    check_in_by=activity.organizer_id if is_checked else None,
                )
            )
            activity.enrolled_count += 1

    # ① 报名中 —— 正常可报名
    sharing = add_activity(
        title="手机摄影技巧分享会",
        category_id=categories["讲座"].id,
        organizer_id=club.id,
        description="面向零基础同学，讲构图、用光与手机自带的后期。\n带上你的手机即可，不需要专业器材。",
        start_time=now + timedelta(days=3, hours=2),
        end_time=now + timedelta(days=3, hours=4),
        enroll_deadline=now + timedelta(days=2),
        location="传媒楼 201",
        capacity=30,
        status=ActivityStatus.PUBLISHED,
        approved_by=admin.id,
        approved_at=now - timedelta(days=1),
    )
    enroll(sharing, all_students[:12])

    # ② 报名中但名额已满 —— 演示「名额已满」的拦截
    workshop = add_activity(
        title="暗房冲洗体验（小班）",
        category_id=categories["文体"].id,
        organizer_id=club.id,
        description="限 5 人小班，讲黑白胶片的显影与定影。",
        start_time=now + timedelta(days=5),
        end_time=now + timedelta(days=5, hours=3),
        enroll_deadline=now + timedelta(days=4),
        location="艺术楼 B1 暗房",
        capacity=5,
        status=ActivityStatus.PUBLISHED,
        approved_by=admin.id,
        approved_at=now - timedelta(hours=6),
    )
    enroll(workshop, all_students[:5])

    # ③ 已发布但报名已截止 —— 活动还没开始，名额也还有，但报不上了
    deadline_passed = add_activity(
        title="校运会摄影志愿服务",
        category_id=categories["志愿"].id,
        organizer_id=club.id,
        description="为校运会提供摄影志愿服务，可计志愿时长。",
        start_time=now + timedelta(days=2),
        end_time=now + timedelta(days=2, hours=8),
        enroll_deadline=now - timedelta(hours=3),
        location="田径场",
        capacity=20,
        status=ActivityStatus.PUBLISHED,
        approved_by=admin.id,
        approved_at=now - timedelta(days=2),
    )
    enroll(deadline_passed, all_students[2:9])

    # ④ 进行中 —— 签到开放，部分人已签到
    #
    # 结束时间刻意放到 6 小时之后，而不是按真实活动的 2 小时来设。
    # 原因：这是唯一一场「进行中」的活动，整条「凭签到码签到」的演示路径都靠它。
    # 原来的 +1 小时会让演示数据「过一小时就失效」——等隔一会儿再打开页面，
    # 它已经变成「已结束」，签到通道关闭，看到的人只会以为功能是坏的。
    # 演示数据要经得起隔几小时再打开，宁可让这场活动的时长不那么真实。
    ongoing = add_activity(
        title="书法体验课",
        category_id=categories["文体"].id,
        organizer_id=club.id,
        description="从执笔与基本笔画开始，两小时写出一幅小作品。",
        start_time=now - timedelta(minutes=30),
        end_time=now + timedelta(hours=6),
        enroll_deadline=now - timedelta(days=1),
        location="学生活动中心 305",
        capacity=20,
        status=ActivityStatus.ONGOING,
        check_in_code="482913",
        approved_by=admin.id,
        approved_at=now - timedelta(days=3),
    )
    enroll(ongoing, all_students[:8], checked_in=5)

    # ⑤ 已结束 —— 数据归档，可以看统计
    finished = add_activity(
        title="校园马拉松",
        category_id=categories["文体"].id,
        organizer_id=other_club.id,
        description="五公里欢乐跑，完赛发纪念徽章。",
        start_time=now - timedelta(days=6),
        end_time=now - timedelta(days=6) + timedelta(hours=3),
        enroll_deadline=now - timedelta(days=8),
        location="田径场",
        capacity=50,
        status=ActivityStatus.FINISHED,
        approved_by=admin.id,
        approved_at=now - timedelta(days=10),
    )
    enroll(finished, all_students[:10], checked_in=8)

    # ⑥ 草稿 —— 只有创建者看得见
    add_activity(
        title="校园夜景采风（草稿）",
        category_id=categories["文体"].id,
        organizer_id=club.id,
        description="还没定时间，先存着。",
        start_time=now + timedelta(days=10),
        end_time=now + timedelta(days=10, hours=3),
        enroll_deadline=now + timedelta(days=9),
        location="待定",
        capacity=15,
        status=ActivityStatus.DRAFT,
    )

    # ⑦ 待审核 —— 演示管理员审核中心的队列
    add_activity(
        title="人像摄影体验日",
        category_id=categories["文体"].id,
        organizer_id=club.id,
        description="请模特到场，讲人像用光与引导。",
        start_time=now + timedelta(days=8),
        end_time=now + timedelta(days=8, hours=3),
        enroll_deadline=now + timedelta(days=7),
        location="传媒楼 201",
        capacity=25,
        status=ActivityStatus.PENDING,
    )
    add_activity(
        title="天文观测夜",
        category_id=categories["讲座"].id,
        organizer_id=other_club.id,
        description="用社团的两台折返望远镜观测土星与月面环形山。",
        start_time=now + timedelta(days=12),
        end_time=now + timedelta(days=12, hours=4),
        enroll_deadline=now + timedelta(days=11),
        location="教学楼天台",
        capacity=30,
        status=ActivityStatus.PENDING,
    )

    # ⑧ 已驳回 —— 创建者能看到原因
    add_activity(
        title="电竞友谊赛",
        category_id=categories["竞赛"].id,
        organizer_id=club.id,
        description="与兄弟院校的线上友谊赛。",
        start_time=now + timedelta(days=15),
        end_time=now + timedelta(days=15, hours=5),
        enroll_deadline=now + timedelta(days=14),
        location="线上",
        capacity=32,
        status=ActivityStatus.REJECTED,
        reject_reason="活动时间与校运会冲突，请调整后重新提交",
    )

    # ⑨ 已取消 —— 只读
    add_activity(
        title="夏夜观星露营",
        category_id=categories["文体"].id,
        organizer_id=other_club.id,
        description="原定在郊区露营观测，因天气原因取消。",
        start_time=now + timedelta(days=20),
        end_time=now + timedelta(days=21),
        enroll_deadline=now + timedelta(days=19),
        location="校外营地",
        capacity=40,
        status=ActivityStatus.CANCELED,
    )

    # ⑩ 管理员直接发布的活动（不经过审核）
    add_activity(
        title="新生心理健康讲座",
        category_id=categories["讲座"].id,
        organizer_id=admin.id,
        description="由心理健康中心老师主讲，面向全体新生。",
        start_time=now + timedelta(days=6),
        end_time=now + timedelta(days=6, hours=2),
        enroll_deadline=now + timedelta(days=5),
        location="大礼堂",
        capacity=300,
        status=ActivityStatus.PUBLISHED,
        approved_by=admin.id,
        approved_at=now,
    )

    db.session.commit()

    return {
        "categories": len(CATEGORIES),
        "users": len(DEMO_ACCOUNTS) + len(EXTRA_STUDENTS),
        "activities": ACTIVITY_COUNT,
    }
