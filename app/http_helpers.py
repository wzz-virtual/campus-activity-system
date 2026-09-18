"""HTTP 层的小工具。

放在独立模块里，是为了让 __init__.py 和 permissions.py 共用同一套判断，
不必互相 import。
"""

from flask import request

# 这些前缀下的接口只对外提供 JSON，不接受 HTML
JSON_ONLY_PREFIXES = ("/api/", "/auth/me")


def wants_json():
    """这个请求希望拿到 JSON 还是 HTML？

    页面请求出错要跳登录页、渲染错误页；接口请求出错要给 JSON —— 否则前端
    拿一整页 HTML 去 JSON.parse，会报一个跟真实原因毫无关系的错。

    刻意只认两个**明确**的信号，不去猜：

    1. 路径前缀命中 ``/api/`` 或 ``/auth/me``；
    2. ``Accept`` 头里写了 ``application/json``，并且**没有**写 ``text/html``。

    为什么不用 ``best_match()`` 之类的协商函数？因为浏览器发的是
    ``text/html,...,*/*;q=0.8``，而 ``*/*`` 会同时匹配两种类型；不带 Accept
    头的请求（比如测试客户端）在协商里也会落回第一个候选。这类写法看起来
    更"专业"，实际会把大量普通页面请求判成 JSON 请求。
    """
    if request.path.startswith(JSON_ONLY_PREFIXES):
        return True

    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept
