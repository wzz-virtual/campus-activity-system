"""演示数据播种（V2.0）。

用途：把数据库初始化成一份**能直接走一遍完整闭环**的数据，省去手工建账号、
建分类、造报名记录的功夫。命令行用法::

    .venv\\Scripts\\python.exe -m flask init-db
    .venv\\Scripts\\python.exe -m flask seed-demo            # 空库初始化
    .venv\\Scripts\\python.exe -m flask seed-demo --reset    # 清空重播

演示数据刻意覆盖了 V2.0 的每一种活动状态与报名状态，以及几种平时很难自然
出现的分支——它们是最需要人工确认的界面场景：

* **满员 + 候补队列**（暗房冲洗体验）：演示「候补中 · 第 N 位」与队列顺序；
* **满员 + 需审核**（胶片冲洗进阶班）：「通过」那一刻没有余位，学生会直接排进候补
  ——演示「通过 ≠ 一定参加」（TC-8）；
* **需审核的活动 + 待审核申请**（人像摄影体验日、天文观测夜、胶片冲洗进阶班）：
  演示教师逐人审核、通过后按余位落位；
* **被管理员下线的活动**（电竞友谊赛）：演示「下线 / 恢复」与学生端不可见；
* **五种报名状态同屏**（用 20230114 登录看「我的报名」）：
  正式 / 候补第 3 位 / 待审核 / 已拒绝 / 已取消；
* **报名已截止**「进行中带签到码」（校运会摄影、书法体验课）。

演示数据的时间都是「相对播种那一刻」算的，放着不动会逐天过期——
过一阵子用 ``seed-demo --reset`` 重播一次即可。
"""

from datetime import timedelta

from .extensions import db
from .mailbox import get_announcement
from .models import (
    Activity,
    ActivityStatus,
    AuditAction,
    AuditLog,
    Category,
    Enrollment,
    EnrollStatus,
    Message,
    MessageRecipient,
    Role,
    User,
    utcnow,
)

# 演示账号统一用这个密码。长度满足密码策略（至少 8 位）。
DEMO_PASSWORD = "Campus-Demo-2026"

# 演示账号
# 角色已是 V2.0 的「活动组织教师」；名字按 V2.0 的角色设定为「X 老师」，
# 这样在界面上一眼能看出教师身份（V1.0 时期叫「小张 / 小林 / 社团」）。
DEMO_ACCOUNTS = [
    ("admin", "陈老师", Role.ADMIN, None),
    ("club001", "张老师", Role.TEACHER, "摄影协会"),
    ("club002", "林老师", Role.TEACHER, "天文社"),
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
ACTIVITY_COUNT = 12

# 演示用的「被下线」原因（TC-14 要求教师端能看到处理标记）
OFFLINE_REASON = "活动时间与校历冲突，待与主办方核实后恢复"


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
        people[student_no] = person
    db.session.flush()

    teacher = people["club001"]  # 摄影协会
    other_teacher = people["club002"]  # 天文社
    admin = people["admin"]
    all_students = [people["20230101"], people["20230102"], people["20230103"], *extra]

    def add_audit_trail(activity, offline_reason=None):
        """按活动的**最终状态**倒推出「本来会留下」的流转日志。

        为什么需要这个：演示数据是把活动直接建成「已取消」「已下线」这些
        状态的，绕过了真实的发布 / 下线操作，于是 AuditLog 一条都没有。
        表现是活动卡上的「处理标记」是空的——明明有一场已下线的活动。
        看着像功能没做，其实只是演示数据不完整。

        轨迹规则（和真实流程一致）：
        * 草稿：没有任何流转，不写；
        * 其余：先有一条「发布」（V2.0 里组织教师自己就是发布者）；
        * 已取消：再加一条「取消活动」；
        * 已下线：再加一条「下线活动」（操作人是管理员）。

        时间刻意**排在当前时刻之前**：日志里出现未来时间会让人以为时钟错了。
        """
        if activity.status == ActivityStatus.DRAFT:
            return
        created = activity.created_at

        def log(action, operator_id, reason=None, at=None):
            db.session.add(
                AuditLog(
                    activity_id=activity.id,
                    operator_id=operator_id,
                    action=action,
                    reason=reason,
                    created_at=at or created,
                )
            )

        log(AuditAction.PUBLISH, activity.organizer_id, at=created - timedelta(days=6))

        if activity.status == ActivityStatus.CANCELED:
            log(
                AuditAction.CANCEL,
                activity.organizer_id,
                reason="因天气原因取消",
                at=created - timedelta(hours=30),
            )
        if activity.status == ActivityStatus.OFFLINE:
            log(
                AuditAction.OFFLINE,
                admin.id,
                reason=offline_reason,
                at=created - timedelta(hours=12),
            )

    def add_activity(offline_reason=None, **kwargs):
        activity = Activity(**kwargs)
        db.session.add(activity)
        db.session.flush()
        add_audit_trail(activity, offline_reason=offline_reason)
        return activity

    def enroll(activity, users, checked_in=0):
        """造「正式」报名记录，并同步维护 enrolled_count。

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
                    status=EnrollStatus.CONFIRMED,
                    enrolled_at=activity.enroll_deadline - timedelta(days=2),
                    check_in_at=activity.start_time + timedelta(minutes=5 + index)
                    if is_checked
                    else None,
                    check_in_by=activity.organizer_id if is_checked else None,
                )
            )
            activity.enrolled_count += 1

    def waitlist(activity, users, base_minutes=100):
        """造「候补」记录。进入候补时间刻意错开，才能看出队列顺序。

        候补**不占名额**（enrolled_count 不动）——这一点必须和真实规则一致，
        否则演示数据和系统行为对不上，看的人会以为候选也在占位。
        """
        for index, person in enumerate(users):
            entered = now - timedelta(minutes=base_minutes - index * 15)
            db.session.add(
                Enrollment(
                    activity_id=activity.id,
                    user_id=person.id,
                    status=EnrollStatus.WAITLISTED,
                    enrolled_at=entered - timedelta(minutes=5),
                    waitlisted_at=entered,
                )
            )

    def apply(activity, users):
        """造「待审核」的报名申请（用于演示教师审核）。"""
        for person in users:
            db.session.add(
                Enrollment(
                    activity_id=activity.id,
                    user_id=person.id,
                    status=EnrollStatus.PENDING,
                    enrolled_at=now - timedelta(hours=20),
                )
            )

    # ① 报名中 —— 正常可报名
    sharing = add_activity(
        title="手机摄影技巧分享会",
        category_id=categories["讲座"].id,
        organizer_id=teacher.id,
        description="面向零基础同学，讲构图、用光与手机自带的后期。\n带上你的手机即可，不需要专业器材。",
        start_time=now + timedelta(days=3, hours=2),
        end_time=now + timedelta(days=3, hours=4),
        enroll_deadline=now + timedelta(days=2),
        location="传媒楼 201",
        capacity=30,
        status=ActivityStatus.PUBLISHED,
    )
    enroll(sharing, all_students[:12])

    # ② 报名中但名额已满 + 候补队列 —— 演示「满员 · 可候补」与排队顺序
    workshop = add_activity(
        title="暗房冲洗体验（小班）",
        category_id=categories["文体"].id,
        organizer_id=teacher.id,
        description="限 5 人小班，讲黑白胶片的显影与定影。",
        start_time=now + timedelta(days=5),
        end_time=now + timedelta(days=5, hours=3),
        enroll_deadline=now + timedelta(days=4),
        location="艺术楼 B1 暗房",
        capacity=5,
        status=ActivityStatus.PUBLISHED,
    )
    enroll(workshop, all_students[:5])
    waitlist(workshop, all_students[11:14])  # 第 1 / 2 / 3 位候补

    # ③ 已发布但报名已截止 —— 活动还没开始，名额也还有，但报不上了
    deadline_passed = add_activity(
        title="校运会摄影志愿服务",
        category_id=categories["志愿"].id,
        organizer_id=teacher.id,
        description="为校运会提供摄影志愿服务，可计志愿时长。",
        start_time=now + timedelta(days=2),
        end_time=now + timedelta(days=2, hours=8),
        enroll_deadline=now - timedelta(hours=3),
        location="田径场",
        capacity=20,
        status=ActivityStatus.PUBLISHED,
    )
    enroll(deadline_passed, all_students[2:9])

    # ④ 进行中 —— 签到开放，部分人已签到
    #
    # 结束时间刻意放到 6 小时之后，而不是按真实活动的 2 小时来设。
    # 原因：这是唯一一场「进行中」的活动，整条「凭签到码签到」的演示路径都靠它。
    # 原来的 +1 小时会让演示数据「过一小时就失效」——等隔一会儿再打开页面，
    # 它已经变成「已结束」，签到通道关闭，看到的人只会以为功能是坏的。
    ongoing = add_activity(
        title="书法体验课",
        category_id=categories["文体"].id,
        organizer_id=teacher.id,
        description="从执笔与基本笔画开始，两小时写出一幅小作品。",
        start_time=now - timedelta(minutes=30),
        end_time=now + timedelta(hours=6),
        enroll_deadline=now - timedelta(days=1),
        location="学生活动中心 305",
        capacity=20,
        status=ActivityStatus.ONGOING,
        check_in_code="482913",
    )
    enroll(ongoing, all_students[:8], checked_in=5)

    # ⑤ 已结束 —— 数据归档，可以看统计
    finished = add_activity(
        title="校园马拉松",
        category_id=categories["文体"].id,
        organizer_id=other_teacher.id,
        description="五公里欢乐跑，完赛发纪念徽章。",
        start_time=now - timedelta(days=6),
        end_time=now - timedelta(days=6) + timedelta(hours=3),
        enroll_deadline=now - timedelta(days=8),
        location="田径场",
        capacity=50,
        status=ActivityStatus.FINISHED,
    )
    enroll(finished, all_students[:10], checked_in=8)

    # ⑥ 草稿 —— 只有创建者看得见
    add_activity(
        title="校园夜景采风（草稿）",
        category_id=categories["文体"].id,
        organizer_id=teacher.id,
        description="还没定时间，先存着。",
        start_time=now + timedelta(days=10),
        end_time=now + timedelta(days=10, hours=3),
        enroll_deadline=now + timedelta(days=9),
        location="待定",
        capacity=15,
        status=ActivityStatus.DRAFT,
    )

    # ⑦ 需审核的活动 + 待审核申请 —— 演示「教师逐人审核、通过不等于有位置」
    portrait = add_activity(
        title="人像摄影体验日",
        category_id=categories["文体"].id,
        organizer_id=teacher.id,
        description="请模特到场，讲人像用光与引导。",
        start_time=now + timedelta(days=8),
        end_time=now + timedelta(days=8, hours=3),
        enroll_deadline=now + timedelta(days=7),
        location="传媒楼 201",
        capacity=25,
        requires_review=True,
        qualification="面向全体在校学生；需自备相机或可拍照的手机。",
        status=ActivityStatus.PUBLISHED,
    )
    apply(portrait, all_students[3:6])

    starry = add_activity(
        title="天文观测夜",
        category_id=categories["讲座"].id,
        organizer_id=other_teacher.id,
        description="用社团的两台折返望远镜观测土星与月面环形山。",
        start_time=now + timedelta(days=12),
        end_time=now + timedelta(days=12, hours=4),
        enroll_deadline=now + timedelta(days=11),
        location="教学楼天台",
        capacity=30,
        requires_review=True,
        qualification="面向天文社成员及对天文感兴趣的在校学生，夜间户外活动请注意保暖。",
        status=ActivityStatus.PUBLISHED,
    )
    apply(starry, all_students[6:7])

    # ⑧ 满员 + 需审核 —— 演示「通过 ≠ 一定参加」（TC-8）：
    #    上限只有 3 人且已经满员，教师审核"通过"的那一刻没有余位，
    #    学生不会变成正式，而是直接排进候补队列。
    advanced = add_activity(
        title="胶片冲洗进阶班（限 3 人 · 需审核）",
        category_id=categories["文体"].id,
        organizer_id=teacher.id,
        description="在暗房冲洗体验课的基础上，完成一轮从拍摄到成片的完整流程。",
        start_time=now + timedelta(days=7),
        end_time=now + timedelta(days=7, hours=3),
        enroll_deadline=now + timedelta(days=6),
        location="艺术楼 B1 暗房",
        capacity=3,
        requires_review=True,
        qualification="已完成「暗房冲洗体验（小班）」或自备黑白胶卷；限 3 人。",
        status=ActivityStatus.PUBLISHED,
    )
    enroll(advanced, [all_students[7], all_students[8], all_students[9]])
    apply(advanced, [all_students[14], all_students[10]])

    # ⑨ 已下线 —— 管理员对违规活动的处理，学生端不可见（TC-14）
    add_activity(
        offline_reason=OFFLINE_REASON,
        title="电竞友谊赛",
        category_id=categories["竞赛"].id,
        organizer_id=teacher.id,
        description="与兄弟院校的线上友谊赛。",
        start_time=now + timedelta(days=15),
        end_time=now + timedelta(days=15, hours=5),
        enroll_deadline=now + timedelta(days=14),
        location="线上",
        capacity=32,
        status=ActivityStatus.OFFLINE,
    )

    # ⑩ 已取消 —— 只读
    add_activity(
        title="夏夜观星露营",
        category_id=categories["文体"].id,
        organizer_id=other_teacher.id,
        description="原定在郊区露营观测，因天气原因取消。",
        start_time=now + timedelta(days=20),
        end_time=now + timedelta(days=21),
        enroll_deadline=now + timedelta(days=19),
        location="校外营地",
        capacity=40,
        status=ActivityStatus.CANCELED,
    )

    # ⑪ 管理员名下的活动（历史数据：V1.0 时期管理员可直接建活动）
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
    )

    # ⑫ 五种报名状态同屏的演示账号 —— 用 20230114（杨帆）登录看「我的报名」：
    #    正式（手机摄影）/ 候补第 3 位（暗房）/ 待审核（人像摄影）/
    #    已拒绝（天文观测夜）/ 已取消（校运会摄影志愿服务），一页看全。
    fan = people["20230114"]
    enroll(sharing, [fan])              # 正式：并入手机摄影的名单（计数同步推进）
    apply(portrait, [fan])              # 待审核：人像摄影体验日的申请
    db.session.add(
        Enrollment(                     # 已拒绝：天文观测夜（带审核时间）
            activity_id=starry.id,
            user_id=fan.id,
            status=EnrollStatus.REJECTED,
            enrolled_at=now - timedelta(days=3),
            reviewed_at=now - timedelta(days=2),
            reviewed_by=other_teacher.id,
        )
    )
    db.session.add(
        Enrollment(                     # 已取消：校运会摄影（报名后在截止前退出）
            activity_id=deadline_passed.id,
            user_id=fan.id,
            status=EnrollStatus.CANCELED,
            enrolled_at=now - timedelta(days=6),
            canceled_at=now - timedelta(days=5),
        )
    )

    # ⑬ 公告与站内邮件（V2.0 的「邮件」功能）
    #    公告默认**不预置文案**（用户要求首页不放说明文字）：
    #    这里只保证公告行存在，内容由教师 / 管理员以后在「邮件」页自己写。
    get_announcement()

    def send(subject, body, recipients, sender):
        message = Message(sender_id=sender.id, subject=subject, body=body)
        db.session.add(message)
        db.session.flush()
        for person in recipients:
            db.session.add(
                MessageRecipient(message_id=message.id, user_id=person.id)
            )
        return message

    # 两封示例邮件：登录任意学生账号，右上角「邮件」就有未读角标可看
    send(
        "本周活动安排提醒",
        "同学们好：\n"
        "本周有三场活动开放报名——「手机摄影技巧分享会」「暗房冲洗体验（小班）」"
        "与「人像摄影体验日」。\n"
        "其中「人像摄影体验日」需要审核，请如实填写报名信息；满员的活动可以先加入"
        "候补队列，有人退出时按先来后到自动递补。\n\n张老师",
        all_students,
        teacher,
    )
    send(
        "平台使用提示",
        "同学们好：\n"
        "忘记密码时，可以在登录页点「忘记密码」提交找回申请（管理员核实身份后重置）；"
        "报名、候补与审核的进度，都可以在「我的报名」里查看。\n"
        "如遇账号异常请直接联系系统管理员。\n\n陈老师",
        all_students,
        admin,
    )

    db.session.commit()

    return {
        "categories": len(CATEGORIES),
        "users": len(DEMO_ACCOUNTS) + len(EXTRA_STUDENTS),
        "activities": ACTIVITY_COUNT,
        "messages": 2,
    }
