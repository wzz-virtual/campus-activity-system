"""真实服务器端到端冒烟。

pytest 里的其它测试都走 Flask 的测试客户端——它把 WSGI 调用短路了，
**测不到** `run.py`、环境变量、`flask` 命令行这几段接线。这个文件专门补那一块：
真的起一个开发服务器，用 HTTP 请求把主链路走一遍。

默认**跳过**（`pytest.ini` 里加了 `-m "not smoke"`），因为它要占端口、
要跑十来秒，不适合塞进每次的快速反馈里。需要时显式跑::

    .venv\\Scripts\\python.exe -m pytest -m smoke
"""

import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

import pytest

from app.seed import DEMO_PASSWORD

pytestmark = pytest.mark.smoke

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
PYTHON = sys.executable


def free_port():
    """让操作系统分一个空闲端口，避免和本机其它服务撞车。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_flask(*args, env):
    return subprocess.run(
        [PYTHON, "-m", "flask", "--app", "run:app", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def csrf_of(html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    assert match, "页面里没有 csrf_token，表单缺少 CSRF 防护"
    return match.group(1)


def quota_of(html):
    """从活动详情页读出「已报 / 上限」。"""
    match = re.search(r"名额</dt>\s*<dd[^>]*>\s*(\d+)\s*/\s*(\d+)", html)
    return (int(match.group(1)), int(match.group(2))) if match else None


@pytest.fixture
def live_server(tmp_path):
    """建库 → 播种 → 起 run.py，返回一个能发请求的小客户端。"""
    database = tmp_path / "smoke.db"
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    env = dict(os.environ)
    # 路径统一用正斜杠：SQLite URL 里的反斜杠会被当成转义字符
    env["DATABASE_URL"] = "sqlite:///" + database.as_posix()
    env["PORT"] = str(port)
    env["PYTHONIOENCODING"] = "utf-8"

    for args in (("init-db",), ("seed-demo",)):
        result = run_flask(*args, env=env)
        assert result.returncode == 0, f"flask {args[0]} 失败：{result.stdout}\n{result.stderr}"

    process = subprocess.Popen(
        [PYTHON, "run.py"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    try:
        for _ in range(60):
            try:
                opener.open(f"{base}/health", timeout=2).read()
                break
            except Exception:
                time.sleep(0.4)
        else:
            pytest.fail("开发服务器没能在 24 秒内起来")

        class Client:
            def get(self, path):
                return opener.open(f"{base}{path}", timeout=10).read().decode("utf-8")

            def raw(self, path):
                return opener.open(f"{base}{path}", timeout=10).read()

            def post(self, path, data):
                body = urllib.parse.urlencode(data).encode()
                return opener.open(
                    f"{base}{path}", data=body, timeout=10
                ).read().decode("utf-8")

            def login(self, student_no):
                self.post(
                    "/auth/login",
                    {
                        "csrf_token": csrf_of(self.get("/auth/login")),
                        "student_no": student_no,
                        "password": DEMO_PASSWORD,
                    },
                )

            def logout(self, current_page):
                self.post("/auth/logout", {"csrf_token": csrf_of(current_page)})

        yield Client()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def test_public_pages_render(live_server):
    assert '"status": "ok"' in live_server.get("/health")

    # 首页只列「最近 4 场」已发布 / 进行中的活动（按开始时间倒序），
    # 挑一场确实排在前四的来断言——种子里开始时间最晚的那场。
    assert "天文观测夜" in live_server.get("/")

    plaza = live_server.get("/activities")
    assert "活动广场" in plaza
    assert "暗房冲洗体验" in plaza


def test_student_can_enroll_and_cancel(live_server):
    # 20230115 是播种数据里没报名活动 1 的学生，
    # 用它才能看到「报名前 / 报名后」的真实变化
    live_server.login("20230115")
    assert "我的报名" in live_server.get("/my/enrollments")

    detail = live_server.get("/activities/1")
    assert quota_of(detail) == (13, 30)

    live_server.post("/activities/1/enroll", {"csrf_token": csrf_of(detail)})
    after = live_server.get("/activities/1")
    assert quota_of(after) == (14, 30), "报名后名额没有 +1"

    live_server.post("/activities/1/unenroll", {"csrf_token": csrf_of(after)})
    assert quota_of(live_server.get("/activities/1")) == (13, 30), "取消后名额没有回退"


def test_full_activity_gives_waitlist_over_real_http(live_server):
    """满员活动报名 → 进入候补并显示排位（V2.0 的主路径，走真实 HTTP 验一遍）。"""
    live_server.login("20230115")  # 没报过暗房（满员：5/5，已有 3 人候补）

    detail = live_server.get("/activities/2")
    assert quota_of(detail) == (5, 5)

    live_server.post("/activities/2/enroll", {"csrf_token": csrf_of(detail)})

    after = live_server.get("/activities/2")
    assert "候补中" in after and "第 4 位" in after, "满员报名应当进入候补并给出排位"
    assert quota_of(after) == (5, 5), "候补不占名额，正式人数不能变"


def test_teacher_roster_and_csv_export(live_server):
    live_server.login("club001")

    roster = live_server.get("/activities/4/enrollments")
    assert "签到码" in roster
    assert "482913" in roster

    payload = live_server.raw("/activities/4/enrollments/export")
    assert payload.startswith(b"\xef\xbb\xbf"), "CSV 缺少 UTF-8 BOM，Excel 打开会乱码"
    assert "序号,状态,学号,姓名".encode("utf-8") in payload

    assert "整体签到率" in live_server.get("/teacher/stats")


def test_admin_supervision_offline_and_restore(live_server):
    """管理员：活动监督（下线 / 恢复）走真实 HTTP 验一遍。"""
    live_server.login("admin")

    supervision = live_server.get("/admin/activities")
    assert "活动监督" in supervision
    assert "手机摄影技巧分享会" in supervision

    # 下线活动 1，学生端立刻不可见
    detail = live_server.get("/activities/1")
    live_server.post(
        "/admin/activities/1/offline",
        {"csrf_token": csrf_of(detail), "reason": "冒烟测试"},
    )
    assert "手机摄影技巧分享会" not in live_server.get("/activities")

    # 恢复后重新可见
    detail = live_server.get("/activities/1")
    live_server.post("/admin/activities/1/restore", {"csrf_token": csrf_of(detail)})
    assert "手机摄影技巧分享会" in live_server.get("/activities")

    assert "分类管理" in live_server.get("/admin/categories")
    assert "用户管理" in live_server.get("/admin/users")


def test_role_switching_works_over_real_http(live_server):
    """三个角色依次登录、各自打开落地页——真实 HTTP 下最容易出问题的一段。"""
    for student_no, landing, marker in [
        ("20230101", "/activities", "活动广场"),
        ("club001", "/my/activities", "我的活动"),
        ("admin", "/admin/activities", "活动监督"),
    ]:
        live_server.login(student_no)
        page = live_server.get(landing)
        assert marker in page, f"{student_no} 打不开 {landing}"

        live_server.logout(page)
