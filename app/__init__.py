"""校园活动管理系统 —— 应用工厂。

用工厂函数而不是模块级全局 app，好处是测试可以创建多个互不干扰的实例
（每个测试一个独立数据库），不会出现测试之间互相污染数据的情况。
"""

import os

import click
from flask import Flask, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from flask_wtf.csrf import CSRFError

from . import timeutils
from .config import CONFIG_MAP
from .extensions import csrf, db, login_manager
from .http_helpers import wants_json

# 每个响应都会带上的安全响应头。
# 这几条成本都只有一行，但能挡掉一整类常见问题。
SECURITY_HEADERS = {
    # 禁止浏览器猜测 Content-Type。否则浏览器可能把一段用户可控的文本
    # 当成脚本执行，把一个「无害的上传」变成 XSS。
    "X-Content-Type-Options": "nosniff",
    # 禁止任何站点用 iframe 嵌套本站页面。防点击劫持：攻击者把后台页面
    # 透明地盖在自己页面上，诱导管理员点到他看不见的按钮。
    "X-Frame-Options": "DENY",
    # 跨站跳转时只发送来源站，不带完整路径（路径里可能有活动 id 之类的信息）
    "Referrer-Policy": "same-origin",
}


def create_app(config_name=None, **overrides):
    """创建并配置 Flask 应用。

    :param config_name: "dev" / "test" / "prod"，默认读环境变量 FLASK_CONFIG
    :param overrides: 需要临时覆盖的配置项，测试里会用它指定临时数据库
    """
    app = Flask(__name__, instance_relative_config=True)

    config_name = config_name or os.environ.get("FLASK_CONFIG", "dev")
    if config_name not in CONFIG_MAP:
        raise ValueError(
            f"未知的配置名 {config_name!r}，可选：{', '.join(CONFIG_MAP)}"
        )

    config_cls = CONFIG_MAP[config_name]
    app.config.from_object(config_cls)

    # 环境自检放在最前面：生产环境缺 SECRET_KEY 这类问题必须在开始服务
    # 之前就炸掉，而不是等被人利用了才发现。
    config_cls.init_app(app)

    # 显式覆盖放在最后，优先级最高 —— 测试就是靠它把数据库换成内存库的。
    # 顺序反了的话，配置类里的 os.environ 取值会把覆盖项冲掉。
    if overrides:
        app.config.update(overrides)

    # 相对路径的 SQLite 会落在 instance/ 目录下，先确保它存在
    os.makedirs(app.instance_path, exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    # 必须在这里 import，让所有表的定义在第一次访问数据库前注册到 db.metadata
    from . import models  # noqa: F401

    @login_manager.user_loader
    def load_user(user_id):
        """Flask-Login 每次请求都会用它把 session 里的 user_id 还原成用户对象。"""
        try:
            return db.session.get(models.User, int(user_id))
        except (TypeError, ValueError):
            return None

    @login_manager.unauthorized_handler
    def unauthorized():
        """未登录时的处理。

        页面请求**跳转到登录页**（并把原本要去的地方带上，登录后跳回来），
        而不是甩一个 401 错误页——用户点"工作台"却看到"请先登录"，还得自己
        再找登录入口，是很别扭的体验。

        接口请求则返回 401 JSON，否则前端拿一整页 HTML 去解析会报出
        跟真实原因毫无关系的错。
        """
        if wants_json():
            return jsonify(error="未登录"), 401
        target = request.path if request.method == "GET" else None
        return redirect(url_for("auth.login", next=target))

    from . import activities, admin, auth, mailbox, main, services

    app.register_blueprint(auth.bp)
    app.register_blueprint(main.bp)
    app.register_blueprint(activities.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(mailbox.bp)

    # ---- 全局请求钩子 ----

    @app.before_request
    def advance_activity_statuses():
        """按时间把到点的活动推进到下一个状态。

        放在这里而不是定时任务里：状态完全由时间决定，读的时候现算一次即可，
        不必为这套系统引入一套调度器依赖。静态文件请求跳过，没必要为一张 CSS
        去动数据库。
        """
        if request.endpoint == "static":
            return None
        services.sync_activity_statuses()
        return None

    # 待改密状态下唯一还能访问的几个端点
    PASSWORD_CHANGE_ALLOWED_ENDPOINTS = {
        "auth.change_password",
        "auth.logout",
        "main.health",
        "static",
    }

    @app.before_request
    def force_password_change():
        """用临时密码登录的用户，在改密码之前什么都做不了。

        只在登录那一步重定向是不够的——用户完全可以敲地址栏绕过去。
        限制必须落在每个请求上。
        """
        if not current_user.is_authenticated:
            return None
        if not getattr(current_user, "must_change_password", False):
            return None
        if request.endpoint in PASSWORD_CHANGE_ALLOWED_ENDPOINTS:
            return None
        return redirect(url_for("auth.change_password"))

    # ---- 响应头 ----

    @app.after_request
    def add_security_headers(response):
        for header, value in SECURITY_HEADERS.items():
            # setdefault：某个视图若已显式设过，就不要覆盖它
            response.headers.setdefault(header, value)
        return response

    # ---- 错误处理 ----

    @app.errorhandler(CSRFError)
    def handle_csrf_error(error):
        """跨站请求伪造校验失败。

        正常用户几乎不会撞上：它意味着这个 POST 不是从本站页面发出的。
        要么页面开着太久导致凭证失效，要么真的有人在伪造请求。
        两种原因刻意不区分，统一提示刷新重试。
        """
        return (
            render_template(
                "errors/message.html",
                code=400,
                message="表单校验失败，可能是页面停留过久导致凭证失效，请刷新后重试。",
            ),
            400,
        )

    @app.errorhandler(400)
    def bad_request(error):
        if wants_json():
            return jsonify(error="请求无效"), 400
        return render_template("errors/message.html", code=400, message="请求无效"), 400

    @app.errorhandler(403)
    def forbidden(error):
        if wants_json():
            return jsonify(error="无权访问"), 403
        return (
            render_template(
                "errors/message.html",
                code=403,
                message="你没有权限访问这个页面",
            ),
            403,
        )

    @app.errorhandler(404)
    def not_found(error):
        if wants_json():
            return jsonify(error="资源不存在"), 404
        return render_template("errors/message.html", code=404, message="页面不存在"), 404

    @app.errorhandler(413)
    def payload_too_large(error):
        return (
            render_template(
                "errors/message.html",
                code=413,
                message="提交的内容太大了，请减少后重试。",
            ),
            413,
        )

    @app.errorhandler(500)
    def internal_error(error):
        """服务端错误兜底（DR-8）：给一个可读页面 + 重试入口，别甩白屏。

        注意：DEBUG / 测试传播异常时不会走到这里，只有生产配置
        （``deploy.py``，DEBUG=False）才真正生效。
        """
        # 出错时事务可能停在半途，先回滚，避免同一个连接把坏状态带给下一个请求
        db.session.rollback()
        if wants_json():
            return jsonify(error="服务器开小差了，请重试"), 500
        return (
            render_template(
                "errors/message.html",
                code=500,
                message="服务器开小差了，请稍后重试。如果反复失败，请把当前操作告诉管理员。",
                retry=True,
            ),
            500,
        )

    @app.context_processor
    def inject_globals():
        """注入所有模板都能用的变量。"""
        # 导航栏右上角的邮件未读角标。每个请求一次 COUNT，对这套规模可以忽略；
        # 没登录时（current_user 是匿名）直接跳过，连查询都不发。
        unread = 0
        if current_user.is_authenticated:
            unread = mailbox.unread_count(current_user.id)
        return {"current_user": current_user, "unread_mail_count": unread}

    # ---- 模板过滤器 ----

    @app.template_filter("localtime")
    def localtime_filter(value, fmt="%m-%d %H:%M"):
        """把数据库里的 UTC 时间转成本地时间再格式化。

        ⚠️ 数据库统一存 UTC，展示时**必须**转换。直接把 UTC 当本地时间显示，
        会让用户看到早 8 小时的时间——这种错误不会报异常、不会崩，
        只会让人在错误的时刻跑到活动场地。属于最难发现的一类缺陷。
        """
        return timeutils.format_local(value, fmt)

    @app.template_filter("localinput")
    def localinput_filter(value):
        """格式化成 ``<input type="datetime-local">`` 能回填的形式。"""
        return timeutils.format_local_input(value)

    @app.template_global()
    def cancel_deadline_text(activity):
        """「还能取消到什么时候」。模板里直接调，避免每个视图都算一遍。"""
        return services.cancel_deadline_text(activity)

    # ---- 命令行工具 ----

    @app.cli.command("init-db")
    def init_db():
        """根据模型建表。"""
        db.create_all()
        print("数据库表已创建")

    @app.cli.command("upgrade-v2")
    def upgrade_v2():
        """把 V1.0 的数据库就地升级到 V2.0（幂等，可重复执行）。

        V2.0 改了角色、活动状态与报名状态的取值，还添了几列。升级过程
        不重建数据库——已有的账号、活动、报名记录一条不动，只做翻译。
        """
        from .migrations import upgrade_to_v2

        notes = upgrade_to_v2()
        if notes:
            print("数据库已升级到 V2.0：")
            for note in notes:
                print(f"  - {note}")
        else:
            print("数据库结构已经是 V2.0，无需迁移。")

    @app.cli.command("seed-demo")
    @click.option(
        "--reset",
        is_flag=True,
        help="先清空全部数据表再重新播种。演示数据是按「相对当前时间」生成的，"
        "放着不动会逐天过期，过一阵子重播一次即可。",
    )
    def seed_demo(reset):
        """写入演示数据：分类、三个角色的账号、覆盖全部状态的活动与报名记录。"""
        from .seed import DEMO_ACCOUNTS, DEMO_PASSWORD, seed_demo_data

        if reset:
            # 演示数据里的活动时间都是「相对播种那一刻」算出来的：
            # 报名截止 +2 天、进行中的活动 +6 小时……放着不动就会逐天过期，
            # 过期之后页面上只剩「已结束」「报名已截止」，看着像功能坏了。
            # 所以给一个一键重播的入口，而不是让人手动去删数据库文件。
            db.drop_all()
            db.create_all()
            print("已清空全部数据表")

        try:
            summary = seed_demo_data()
        except RuntimeError as exc:
            # 转成 ClickException，命令行里就是一句干净的报错，
            # 而不是一长串 Python 调用栈
            raise click.ClickException(str(exc))

        print(
            f"已写入演示数据：{summary['categories']} 个分类、"
            f"{summary['users']} 个账号、{summary['activities']} 场活动。"
        )
        print(f"\n所有演示账号的密码都是：{DEMO_PASSWORD}\n")
        for student_no, name, role, club_name in DEMO_ACCOUNTS:
            suffix = f"（{club_name}）" if club_name else ""
            print(f"  {role:<8} {student_no:<10} {name}{suffix}")

    @app.cli.command("create-user")
    @click.option("--student-no", required=True, help="学号 / 工号，同时作为登录账号")
    @click.option("--name", required=True, help="姓名")
    @click.option("--password", required=True, help="初始密码")
    @click.option(
        "--role",
        default=models.Role.STUDENT,
        type=click.Choice(list(models.Role.ALL), case_sensitive=False),
        help="角色，默认学生",
    )
    def create_user(student_no, name, password, role):
        """创建一个账号，用于在本地验证登录与权限流程。

        学生可以自助注册（/auth/register）；教师 / 管理员账号没有自助入口，
        只能由管理员在这里或后台创建——角色永远不由客户端决定。
        """
        exists = db.session.execute(
            db.select(models.User).where(models.User.student_no == student_no)
        ).scalar_one_or_none()
        if exists:
            raise click.ClickException(f"学号 / 工号 {student_no} 已存在")

        user = models.User(student_no=student_no, name=name, role=role.upper())
        try:
            user.set_password(password)
        except ValueError as exc:
            raise click.ClickException(str(exc))

        db.session.add(user)
        db.session.commit()
        print(f"已创建：{user.student_no} {user.name}（{user.role_label}）")

    return app
