"""线上发布用的启动入口。

和 ``run.py`` 的区别，都是发布环境要求的：

* **绑定 ``0.0.0.0``**，端口取环境变量 ``PORT``。线上沙箱会注入 ``PORT``，
  只监听回环地址的话反向代理根本连不上。
* **用生产配置**（``FLASK_CONFIG=prod``）：关掉 DEBUG。这一点是硬要求——
  开发配置会打开 Werkzeug 的交互式调试控制台，那东西放在公网上等于把
  任意代码执行的能力开放给所有人。
* **启动时自己建表并铺演示数据**（见 ``app/bootstrap.py``）。沙箱里没有运维
  去跑 ``init-db`` / ``seed-demo``。
* **数据库单独一个文件**（``instance/live.db``），和本地开发用的
  ``instance/campus.db`` 分开：这样线上不会被本地那份「已经放了三周的旧数据」
  污染，本地调试也不会误改线上数据。

启动::

    python deploy.py                # 本地模拟：PORT=5000
    PORT=8080 python deploy.py      # 线上由沙箱注入 PORT

注意：用的是 Flask 自带的开发服务器（单进程 + 多线程）。对课程演示站足够，
但它不是为公网设计的——真要长期对外提供服务，应该换成 waitress / gunicorn。
"""

import os
import secrets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def configure_environment():
    """把线上必需的环境变量补齐。

    ``ProdConfig`` 会拒绝「缺 SECRET_KEY」或「用开发占位值」的启动——这个
    自检本身是对的，所以这里不去绕过它，而是补一个**真正的随机密钥**。

    代价要说清楚：密钥每次启动都变，意味着重启之后所有人的登录会话都会失效
    （Cookie 的签名对不上）。演示站可以接受；真实部署必须从环境变量注入一个
    固定的密钥，否则每次发布都要所有人重新登录。
    """
    os.environ.setdefault("FLASK_CONFIG", "prod")
    os.environ.setdefault("SECRET_KEY", secrets.token_hex(32))
    os.environ.setdefault(
        "DATABASE_URL",
        "sqlite:///" + os.path.join(BASE_DIR, "instance", "live.db").replace("\\", "/"),
    )


def build_app():
    configure_environment()

    from app import create_app
    from app.bootstrap import prepare_database

    application = create_app()

    # 生产配置默认把 session Cookie 标成 Secure（只允许在 HTTPS 上传输）。
    # 但如果站点实际是按 HTTP 访问的，浏览器和任何客户端**都不会回传**这个
    # Cookie，session 直接丢——表现出来是「一提交登录就 400 BAD REQUEST」
    # （CSRF token 对不上），或者「登录了但一直是未登录状态」。
    # 这个现象和「密码错了」「CSRF 配错了」都很像，很容易查偏。
    #
    # 正常情况（反向代理对外是 HTTPS）保持开启即可；确认是 HTTP 时才关掉：
    #     SESSION_COOKIE_SECURE=0 python deploy.py
    application.config["SESSION_COOKIE_SECURE"] = (
        os.environ.get("SESSION_COOKIE_SECURE", "1") != "0"
    )

    summary = prepare_database(application)
    if summary:
        print(
            f"[启动] 空库已初始化：{summary['categories']} 个分类、"
            f"{summary['users']} 个账号、{summary['activities']} 场活动"
        )
    else:
        print("[启动] 库中已有数据，未改动")
    print(f"[启动] session Cookie 的 Secure 标志：{application.config['SESSION_COOKIE_SECURE']}")
    return application


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print(f"[启动] 监听 0.0.0.0:{port}")
    build_app().run(
        host="0.0.0.0",
        port=port,
        debug=False,          # 公网上绝不能开调试器：那是任意代码执行
        use_reloader=False,
        threaded=True,
    )
