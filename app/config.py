"""应用配置。

不同环境用不同的配置类，通过环境变量 FLASK_CONFIG 选择（默认 dev）。

两条原则：

1. 敏感项一律从环境变量读取，不写死在代码里；
2. 生产环境缺少关键配置时**直接拒绝启动**，而不是拿默认值兜底。
   带病启动比启动失败危险得多——启动失败会立刻被人发现，而一个用着
   公开默认密钥的生产环境，可能几个月都没人注意到。
"""

import logging
import os
from datetime import timedelta

# 项目根目录（本文件在 app/ 下，所以取上一级）
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


# ---------------------------------------------------------------------------
# 安全策略参数：集中放在一处，调策略只改这里
# ---------------------------------------------------------------------------

# 会话有效期。校园系统按"一天登录一次"设计足够；再长就等于"一次登录永久有效"，
# Cookie 一旦泄露，危害窗口会被无限放大。
SESSION_LIFETIME = timedelta(hours=8)

# 连续登录失败达到这个次数，账号临时锁定
LOGIN_MAX_ATTEMPTS = 5

# 锁定时长。刻意定得短：锁太久反而给了攻击者一个"用错误密码把真实用户
# 挡在门外"的拒绝服务手段。宁可解锁快一点，也不要让正常用户用不了系统。
LOGIN_LOCK_DURATION = timedelta(minutes=15)

# 密码最小长度
MIN_PASSWORD_LENGTH = 8

# 请求体大小上限。目前没有文件上传，4MB 已经很宽松，只是防止超大请求撑爆内存。
MAX_CONTENT_LENGTH_BYTES = 4 * 1024 * 1024


class BaseConfig:
    """所有环境共用的配置。"""

    # 用于给 session Cookie 签名。子类必须覆盖成安全的值。
    SECRET_KEY = None

    # 关掉这个没用的信号追踪，否则每次请求都会有一次额外开销
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # 展示层用的时区；数据库里统一存 UTC
    TIMEZONE = "Asia/Shanghai"

    # 列表页每页条数
    PAGE_SIZE = 10

    # ---- Cookie 与会话安全 ----
    # 让浏览器端的 JavaScript 读不到 session Cookie。
    # 这一条主要防 XSS：即使页面被注入脚本，也偷不走登录态。
    SESSION_COOKIE_HTTPONLY = True

    # 限制跨站请求携带本 Cookie。Lax 的含义是：普通跳转链接会带上，
    # 但跨站的 POST / iframe / fetch 不会——正好挡住大部分 CSRF。
    # Flask 默认不设这个属性，属于我们要主动补上的一层。
    SESSION_COOKIE_SAMESITE = "Lax"

    # 子类覆盖：生产环境必须为 True（只走 HTTPS）
    SESSION_COOKIE_SECURE = False

    PERMANENT_SESSION_LIFETIME = SESSION_LIFETIME

    # Flask-Login 的"记住我"Cookie 目前没用到，但标志位一并设好，
    # 免得将来打开 remember=True 时忘了加固。
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = False

    # ---- 请求体大小 ----
    MAX_CONTENT_LENGTH = MAX_CONTENT_LENGTH_BYTES

    # ---- CSRF ----
    WTF_CSRF_ENABLED = True
    # 默认 1 小时过期：表单页面开久了再提交就会报错，对使用者不友好。
    # 设为 None 表示 token 跟随会话有效期，登录失效时一起失效。
    WTF_CSRF_TIME_LIMIT = None

    @classmethod
    def init_app(cls, app):
        """应用创建后做环境自检。子类可覆盖，不合格就抛异常拒绝启动。"""


class DevConfig(BaseConfig):
    DEBUG = True
    # 这只是为了让本地开箱能跑；init_app 里会优先采用环境变量里的值
    SECRET_KEY = "dev-only-insecure-key-please-set-SECRET_KEY"

    # 相对路径的 SQLite 会被解析到项目的 instance/ 目录下，即 instance/campus.db
    SQLALCHEMY_DATABASE_URI = "sqlite:///campus.db"

    @classmethod
    def init_app(cls, app):
        key = os.environ.get("SECRET_KEY")
        if key:
            app.config["SECRET_KEY"] = key
        else:
            app.logger.warning(
                "当前使用的是开发用默认 SECRET_KEY，仅本地调试可接受；"
                "部署前务必设置环境变量 SECRET_KEY。"
            )

        app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
            "DATABASE_URL", "sqlite:///campus.db"
        )


class TestConfig(BaseConfig):
    TESTING = True
    SECRET_KEY = "test-only-secret-key-not-for-production"

    # 内存数据库，跑完测试就没了
    SQLALCHEMY_DATABASE_URI = "sqlite://"

    # 注意：这里**没有**关掉 CSRF。测试要跑真实链路，否则
    # "表单到底有没有校验 token"这件事永远测不到。
    # 需要的时候由测试自己取 token，而不是把防护关掉。


class ProdConfig(BaseConfig):
    DEBUG = False

    # 刻意不给默认值：拿不到就是 None，由 init_app 直接拒绝启动
    SECRET_KEY = None
    SQLALCHEMY_DATABASE_URI = ""

    # 生产必须走 HTTPS，Cookie 只允许在加密连接上传输
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True

    @classmethod
    def init_app(cls, app):
        # 在 init_app 里读环境变量，而不是写成类属性。
        # 类属性在 import 那一刻就求值了，之后环境变量怎么变都不会反映进去——
        # 在测试和容器化部署里这会非常容易踩坑。
        app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
        app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "")

        errors = []

        key = app.config.get("SECRET_KEY")
        if not key:
            errors.append(
                "缺少环境变量 SECRET_KEY。它用来给 session Cookie 签名，"
                "一旦是空值或公开的默认值，任何人都能伪造出「已登录的管理员」。\n"
                '  生成方式：python -c "import secrets; print(secrets.token_hex(32))"'
            )
        elif key.startswith("dev-only") or key.startswith("test-only"):
            errors.append("SECRET_KEY 用的是开发/测试占位值，不能用于生产环境。")
        elif len(key) < 32:
            errors.append(
                f"SECRET_KEY 太短（当前 {len(key)} 个字符，至少需要 32 个）。"
                "短密钥更容易被暴力猜解。"
            )

        if not app.config.get("SQLALCHEMY_DATABASE_URI"):
            errors.append("缺少环境变量 DATABASE_URL，生产环境不应使用默认的本地 SQLite 文件。")

        if errors:
            raise RuntimeError(
                "生产环境配置自检未通过，拒绝启动：\n- " + "\n- ".join(errors)
            )

        logging.getLogger(__name__).info("生产环境配置自检通过。")


CONFIG_MAP = {
    "dev": DevConfig,
    "test": TestConfig,
    "prod": ProdConfig,
}
