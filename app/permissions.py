"""权限控制。

设计方案的硬性要求：**每个写接口都必须在服务端校验角色与归属权**。
前端把按钮隐藏起来不算权限控制——改个请求就能绕过。

这里提供两层：

* ``role_required``  —— 校验角色（学生 / 社团 / 管理员）
* ``owner_required`` —— 在角色之上再校验资源归属（只能动自己创建的）

约定：**不要把 ``current_user.role == ...`` 散写在各处视图里**。权限判断一旦
分散，迟早会漏掉一个接口。统一走装饰器之后，「没加装饰器的写接口」就等价于
「忘了加权限」，在代码审查时一眼能看出来。
"""

from functools import wraps

from flask import abort, jsonify, redirect, request, url_for
from flask_login import current_user

from .http_helpers import wants_json
from .models import Role


def _deny_anonymous():
    """未登录时的响应：页面请求跳登录页，接口请求给 401 JSON。

    next 参数只在 GET 请求时带上——把用户送到一个只接受 POST 的地址，
    登录完会撞上 405，体验很莫名其妙。
    """
    if wants_json():
        return jsonify(error="未登录"), 401
    target = request.path if request.method == "GET" else None
    return redirect(url_for("auth.login", next=target))


def role_required(*roles):
    """限定只有指定角色可以访问。

    用法::

        @bp.get("/admin/")
        @role_required(Role.ADMIN)
        def admin_home():
            ...

    未登录 → 跳登录页（GET 请求会带 next 参数，登录后跳回来）
    已登录但角色不符 → 403
    """

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return _deny_anonymous()
            if current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def owner_required(get_owner_id):
    """校验「只有资源的所有者或管理员能操作」。

    get_owner_id 是一个函数，接收调用视图时的参数，返回该资源拥有者的 user.id。

    ⚠️ 资源不存在时它**必须抛 404**，而不是返回 None。返回 None 会让校验
    变成 403，等于告诉对方「这个 id 不存在」——而 404 和 403 的区别本身
    就可能被用来探测数据。

    用法::

        @bp.post("/activities/<int:activity_id>/edit")
        @owner_required(lambda activity_id: get_activity_or_404(activity_id).organizer_id)
        def edit_activity(activity_id):
            ...
    """

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return _deny_anonymous()
            # 管理员对全部资源放行；其他角色必须是自己创建的
            if current_user.role != Role.ADMIN:
                owner_id = get_owner_id(*args, **kwargs)
                if owner_id != current_user.id:
                    abort(403)
            return view(*args, **kwargs)

        return wrapper

    return decorator
