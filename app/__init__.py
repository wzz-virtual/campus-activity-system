"""校园活动管理系统 —— 应用工厂。

用工厂函数而不是模块级全局 app，好处是测试可以创建多个互不干扰的实例
（每个测试一个独立数据库），不会出现测试之间互相污染数据的情况。
"""

import os
from datetime import timedelta, timezone

import click
from flask import Flask, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from flask_wtf.csrf import CSRFError

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

    # 必须在这里 import，让五张表的定义在第一次访问数据库前注册到 db.metadata
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

    from . import admin, auth, main

    app.register_blueprint(auth.bp)
    app.register_blueprint(main.bp)
    app.register_blueprint(admin.bp)

    # ---- 全局请求钩子 ----

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

    @app.context_processor
    def inject_globals():
        """注入所有模板都能用的变量。"""
        return {"current_user": current_user}

    # ---- 模板过滤器 ----

    # 展示用时区。中国标准时间全年都是 UTC+8，没有夏令时，
    # 因此用固定偏移就够了，不必为这一个时区引入 tzdata 依赖
    # （Windows 不自带 IANA 时区库，用 ZoneInfo 还得额外装包）。
    display_timezone = timezone(timedelta(hours=8))

    @app.template_filter("localtime")
    def localtime_filter(value, fmt="%m-%d %H:%M"):
        """把数据库里的 UTC 时间转成本地时间再格式化。

        ⚠️ 数据库统一存 UTC，展示时**必须**转换。直接把 UTC 当本地时间显示，
        会让用户看到早 8 小时的时间——这种错误不会报异常、不会崩，
        只会让人在错误的时刻跑到活动场地。属于最难发现的一类缺陷。
        """
        if value is None:
            return "—"
        # 存进去的是不含时区信息的 UTC，这里显式补上 UTC 再转换
        return value.replace(tzinfo=timezone.utc).astimezone(display_timezone).strftime(fmt)

    # ---- 命令行工具 ----

    @app.cli.command("init-db")
    def init_db():
        """根据模型建表。"""
        db.create_all()
        print("数据库表已创建")

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

        系统没有自助注册入口，账号一律由管理员创建——校园场景下这是对的，
        开放注册等于任何人都能给自己开一个号。
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
