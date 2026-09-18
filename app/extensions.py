"""Flask 扩展对象。

这些插件需要先创建"空壳"，再在 create_app() 里绑定到具体的 app 上。
单独放一个模块，是为了避免 __init__.py 和 models.py 互相 import 变成死循环。
"""

from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()

login_manager = LoginManager()

# CSRF 防护：对所有 POST / PUT / PATCH / DELETE 请求校验 token。
# 表单里必须带 csrf_token 隐藏字段，否则请求会被拒绝。
csrf = CSRFProtect()

# 未登录访问受保护页面时的默认跳转目标
login_manager.login_view = "auth.login"
login_manager.login_message = "请先登录"
login_manager.login_message_category = "warning"

# 会话保护级别。
#
# Flask-Login 会给每个会话记录一个指纹（客户端 IP + User-Agent 的哈希），
# 后续请求指纹不一致时：
#   "basic"  —— 把会话标记为「不新鲜」，但保持登录；敏感操作可以要求 fresh
#   "strong" —— 直接销毁会话，要求重新登录
#
# 这里选 "strong"：识别被窃取的 Cookie 更果断。
#
# ⚠️ 代价：手机在 WiFi 和 4G 之间切换、或运营商更换出口 IP 时，指纹会变，
# 用户会被强制重新登录。校园场景里学生大量使用移动网络，这一点需要你自己
# 权衡——如果发现掉线太频繁，改成 "basic"，然后对改密码、审核这类敏感操作
# 单独用 @fresh_login_required 兜底。
login_manager.session_protection = "strong"
