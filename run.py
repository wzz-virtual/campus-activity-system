"""开发服务器入口。

用法::

    .venv\\Scripts\\python.exe run.py

生产环境**不要**用这个文件启动。应该交给 WSGI 服务器，并且先设好环境变量::

    set SECRET_KEY=<用下面这条命令生成>
    set FLASK_CONFIG=prod

    python -c "import secrets; print(secrets.token_hex(32))"

然后用 gunicorn / uwsgi 之类托管 ``app:create_app('prod')``。
"""

import os

from app import create_app

app = create_app()


if __name__ == "__main__":
    # 默认只监听本机回环地址。开发机上随手启动的服务器不该被同网段的
    # 其他人访问到——那等于把你本地的数据库和调试信息暴露出去。
    # 需要让手机连本机调试时，显式设 HOST=0.0.0.0。
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
    )
