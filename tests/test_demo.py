"""Demo 静态原型的验证测试（V2.0）。

`demo/index.html` 是单文件前端原型，界面全部由 JS 拼接生成。所以**不能**靠"正则搜
源码"来验证 —— 源码里根本没有 `data-act="enroll"` 这样的字面量，那种测试只能证明
"文件里出现过某个词"，是假验证。

真正的行为验证放在 `tests/demo_harness.cjs`：它用一套极简 DOM 桩把 Demo 在 Node 里
跑起来，按真实用户的操作顺序派发点击事件，对渲染出来的 HTML 断言 —— 报名后人数是否
+1、满员时是否进入候补并给出排位、退出候补后后面的人是否前移、审核通过后是否按余位
落到正式或候补、取消后队首是否自动递补、导出的 CSV 行数是否等于报名人数 + 1。

本文件负责两件事：

1. **可交付性检查** —— 结构完整、离线自足（双击就能开，不依赖网络）、没有半成品标记；
2. **调用走查脚本** —— 确认全部行为断言通过，并检查走查本身的覆盖面没有被削减。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEMO_FILE = ROOT / "demo" / "index.html"
HARNESS = Path(__file__).resolve().parent / "demo_harness.cjs"

# 走查必须覆盖的环节。少一个就说明覆盖被削减了，这里会拦下来。
REQUIRED_SECTIONS = [
    "登录页",
    "学生：广场与状态标记",
    "学生：报名与取消",
    "学生：满员的三条路（候补 / 正式 / 加入候补）",
    "学生：报名与取消的拦截边界",
    "学生：我的报名",
    "教师：我的活动与直接发布",
    "教师：报名审核（通过 ≠ 正式）",
    "学生：取消触发自动递补",
    "教师：签到核验与名单导出",
    "教师：数据统计",
    "管理员：活动监督（下线 / 恢复）",
]

MIN_CHECKS = 110


@pytest.fixture(scope="module")
def html() -> str:
    assert DEMO_FILE.is_file(), f"Demo 文件不存在：{DEMO_FILE}"
    return DEMO_FILE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 1. 可交付性
# --------------------------------------------------------------------------


def test_demo_file_is_present(html: str) -> None:
    assert len(html) > 20000, f"Demo 文件过小（{len(html)} 字符），可能被截断了"


def test_html_structure(html: str) -> None:
    assert html.lstrip().startswith("<!DOCTYPE html>"), "缺少 DOCTYPE 声明"
    assert 'charset="UTF-8"' in html, "缺少 UTF-8 编码声明，中文会乱码"
    assert 'lang="zh-CN"' in html, "没有声明页面语言"
    assert 'name="viewport"' in html, "缺少 viewport，手机浏览器上会缩放异常"
    assert "<title>" in html and "</title>" in html, "缺少 <title>"
    assert html.rstrip().endswith("</html>"), "</html> 之后有残留内容"


def test_demo_is_offline_self_contained(html: str) -> None:
    """Demo 要能双击直接打开，因此不能引用任何外部资源。

    引了 CDN 的 CSS/JS 或外链图片，断网或校园网拦外域时就白屏。
    """
    external = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    assert not external, f"Demo 引用了外部资源，无法离线打开：{external}"

    assert "<link" not in html, "Demo 不应引用外部样式表"
    assert "@import" not in html, "Demo 不应通过 @import 引入外部样式"


def test_no_unresolved_marker(html: str) -> None:
    for marker in ("TODO", "FIXME", "XXX"):
        assert marker not in html, f"文件里残留了未处理的标记：{marker}"


def test_script_is_strict_and_scoped(html: str) -> None:
    script = max(
        re.findall(r"<script>(.*?)</script>", html, re.S), key=len, default=""
    )
    assert script, "没有找到内嵌的 <script> 块"
    assert "'use strict'" in script, "脚本没有启用严格模式"
    assert "(function () {" in script, "脚本没有用 IIFE 包裹，变量会泄漏到全局"
    assert "</scr" + "ipt>" not in script, "脚本内容里出现了会提前闭合标签的字符串"


# --------------------------------------------------------------------------
# 2. 行为走查
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def walkthrough() -> subprocess.CompletedProcess:
    node = shutil.which("node")
    if not node:
        pytest.skip("本机没有 node，无法执行 Demo 行为走查")
    assert HARNESS.is_file(), f"走查脚本不存在：{HARNESS}"

    return subprocess.run(
        [node, str(HARNESS)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_walkthrough_has_no_failures(walkthrough: subprocess.CompletedProcess) -> None:
    assert walkthrough.returncode == 0, (
        "Demo 行为走查未通过：\n" + walkthrough.stdout + "\n" + walkthrough.stderr
    )


def test_walkthrough_output_is_complete(
    walkthrough: subprocess.CompletedProcess,
) -> None:
    out = walkthrough.stdout

    assert "失败 0 项" in out, "走查输出里没有「失败 0 项」的结论"

    match = re.search(r"通过 (\d+) 项，失败 (\d+) 项", out)
    assert match, f"没有解析到走查统计行：\n{out}"
    passed = int(match.group(1))
    assert passed >= MIN_CHECKS, (
        f"走查只覆盖了 {passed} 项，少于预期的 {MIN_CHECKS} 项 —— 覆盖面被削减了"
    )


def test_walkthrough_covers_all_core_flows(
    walkthrough: subprocess.CompletedProcess,
) -> None:
    """防止有人为了让测试变绿，把走查里的环节删掉。"""
    out = walkthrough.stdout
    missing = [s for s in REQUIRED_SECTIONS if s not in out]
    assert not missing, f"走查缺少这些环节的覆盖：{missing}"


def test_walkthrough_flags_are_all_green(
    walkthrough: subprocess.CompletedProcess,
) -> None:
    """输出里不允许出现失败标记。"""
    assert "\u2717" not in walkthrough.stdout, (
        "走查输出里存在失败项：\n" + walkthrough.stdout
    )
