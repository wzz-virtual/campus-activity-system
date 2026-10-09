"""tools/serve.py 的测试。

这个脚本是以后台方式跑的——它坏掉的时候**不会有任何提示**，只是「网站打不开」。
所以重点不是覆盖率，而是钉住三个「坏起来会很难查」的地方：

* 探活不能把被探测的进程杀掉（Windows 上 ``os.kill(pid, 0)`` 会终止进程）；
* 项目根目录要能自己找对（写死层数的话，文件一挪位置就报 ModuleNotFoundError）；
* 日志与 PID 文件默认必须在项目**之外**，否则会被 Flask 的自动重载器监视到，
  变成「写日志 → 触发重启 → 再写日志」的死循环。
"""

import os
import subprocess
import sys
import tempfile

import pytest

from tools import serve


@pytest.fixture(autouse=True)
def isolate_state_files(monkeypatch, tmp_path):
    """把 PID / 日志文件指向临时目录，并把原始值交给用例备查。

    必须隔离：否则测试会去改真实的状态文件，而 stop 会照着里面的 PID
    真的去杀进程——测试里绝不能出现这种事。
    """
    originals = (serve.LOG_PATH, serve.PID_PATH)
    monkeypatch.setattr(serve, "LOG_PATH", str(tmp_path / "server.log"))
    monkeypatch.setattr(serve, "PID_PATH", str(tmp_path / "state.json"))
    return originals


def write_state(pid, port=5000):
    with open(serve.PID_PATH, "w", encoding="utf-8") as handle:
        handle.write(f'{{"pid": {pid}, "port": {port}}}')


class FakeChild:
    """只用于替换 _spawn 的假子进程。"""

    returncode = None

    def __init__(self, pid=4242):
        self.pid = pid

    def poll(self):
        return None


# ---------------------------------------------------------------------------
# 根目录定位
# ---------------------------------------------------------------------------


def test_root_is_resolved_from_a_marker_not_from_directory_depth():
    """项目根目录按标志文件找，而不是按「往上退几层」。

    写死层数的表现在一开始很不明显：这个脚本从 .workbuddy/tools/ 挪到 tools/
    之后，向上多退了一层，子进程起来就报 ``ModuleNotFoundError: No module
    named 'app'``。它是后台跑的，不去翻日志根本看不到。
    """
    assert os.path.isfile(os.path.join(serve.ROOT, "run.py"))
    assert os.path.isdir(os.path.join(serve.ROOT, "app"))

    for start in (os.path.join(serve.ROOT, "tools"), os.path.join(serve.ROOT, "app")):
        assert serve._find_root(start) == serve.ROOT


def test_find_root_raises_when_there_is_no_project_root(tmp_path):
    """找不到标志文件时要明确报错，不能悄悄返回一个错的目录。"""
    lonely = tmp_path / "a" / "b" / "c"
    lonely.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="找不到项目根目录"):
        serve._find_root(str(lonely))


# ---------------------------------------------------------------------------
# 探活：这件事做错会很严重
# ---------------------------------------------------------------------------


def test_process_alive_does_not_kill_the_process_it_probes():
    """探活必须是只读的。

    Windows 上 ``os.kill(pid, 0)`` 不是「发一个信号 0」——它会走到
    TerminateProcess，等于**探活就把进程杀了**。这个函数要是照抄类 Unix 的
    写法，status 和 stop 就变成了随机杀进程。
    """
    assert serve.process_alive(os.getpid()) is True
    # 上面那句要是真把当前进程杀了，下面这句根本执行不到
    assert serve.process_alive(os.getpid()) is True


def test_process_alive_is_false_for_a_process_that_has_exited():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    # 进程已退出，但 PID 可能还被句柄占着；要靠 GetExitCodeProcess 给结论
    assert serve.process_alive(child.pid) is False


def test_process_alive_is_false_for_a_pid_that_never_existed():
    assert serve.process_alive(999_999_999) is False


# ---------------------------------------------------------------------------
# start / status / stop
# ---------------------------------------------------------------------------


def test_status_is_safe_when_nothing_was_ever_started(monkeypatch, capsys):
    monkeypatch.setattr(serve, "port_open", lambda *a, **k: False)
    assert serve.read_state() == {}
    assert serve.status() == 1
    assert "（无记录）" in capsys.readouterr().out


def test_stop_cleans_up_a_stale_pid_file(capsys):
    """记录的进程早就没了（比如被系统回收），stop 要清理记录而不是报错退出。"""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    write_state(child.pid)

    assert serve.stop() == 0
    assert not os.path.exists(serve.PID_PATH)
    assert "已经不在了" in capsys.readouterr().out


def test_start_refuses_when_the_port_is_already_taken(monkeypatch, capsys):
    """端口被占时要说清楚，而不是起一个必然失败的进程。"""
    monkeypatch.setattr(serve, "port_open", lambda *a, **k: True)
    assert serve.start() == 1
    assert "已被别的进程占用" in capsys.readouterr().out


def test_start_waits_until_the_port_actually_listens(monkeypatch, capsys):
    """启动成功与否以「端口真的开始监听」为准，不能只看启动命令返回 0。"""
    monkeypatch.setattr(serve, "_spawn", lambda flags: FakeChild())
    # 前两次说没在听、第三次才在听——模拟「进程起来了但还没绑端口」
    answers = iter([False, False, True])
    monkeypatch.setattr(serve, "port_open", lambda *a, **k: next(answers, True))
    monkeypatch.setattr(serve.time, "sleep", lambda _: None)

    assert serve.start() == 0
    output = capsys.readouterr().out
    assert "已启动" in output and "127.0.0.1:5000" in output
    assert serve.read_state()["pid"] == 4242


def test_start_uses_detached_process_flag(monkeypatch):
    """DETACHED_PROCESS(0x08) 必须带上。

    不带的话新进程会继承调用方的控制台：那个控制台一关，服务就跟着走了——
    而「关掉终端它还在跑」正是用这个脚本而不是直接跑 run.py 的理由。
    """
    captured = {}

    def fake_spawn(flags):
        captured["flags"] = flags
        return FakeChild()

    monkeypatch.setattr(serve, "_spawn", fake_spawn)
    # 第一次探测是「端口是不是被占了」的准入检查，必须返回 False 才走得下去；
    # 之后返回 True 表示服务已经起来了
    answers = iter([False, True])
    monkeypatch.setattr(serve, "port_open", lambda *a, **k: next(answers, True))
    monkeypatch.setattr(serve.time, "sleep", lambda _: None)

    assert serve.start() == 0
    assert captured["flags"] & 0x00000008


def test_start_reports_the_log_tail_when_the_child_dies_immediately(monkeypatch, capsys):
    """子进程一起来就退出时，要把日志末尾带出来，而不是只说一句「启动失败」。"""

    class DeadChild(FakeChild):
        returncode = 1

        def poll(self):
            return 1

    monkeypatch.setattr(serve, "_spawn", lambda flags: DeadChild())
    monkeypatch.setattr(serve, "port_open", lambda *a, **k: False)
    with open(serve.LOG_PATH, "w", encoding="utf-8") as handle:
        handle.write("ModuleNotFoundError: No module named 'app'\n")

    assert serve.start() == 1
    output = capsys.readouterr().out
    assert "立刻退出" in output
    assert "No module named 'app'" in output


def test_start_is_idempotent_when_already_running(monkeypatch, capsys):
    monkeypatch.setattr(serve, "process_alive", lambda pid: True)
    write_state(4242)

    assert serve.start() == 0
    assert "已经在跑了" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 双击启动的批处理
# ---------------------------------------------------------------------------

BAT_FILES = ("启动系统.bat", "停止系统.bat")


@pytest.mark.parametrize("name", BAT_FILES)
def test_launchers_exist_and_call_the_serve_tool(name):
    path = os.path.join(serve.ROOT, name)
    assert os.path.isfile(path), f"{name} 不见了；双击启动这条路就断了"

    text = open(path, "rb").read().decode("ascii")
    assert "tools" in text and "serve.py" in text


@pytest.mark.parametrize("name", BAT_FILES)
def test_launchers_are_crlf_only(name):
    """批处理必须用 CRLF 行尾。

    只有 LF 时 cmd.exe 会把相邻各行粘在一起解析，整个脚本直接崩，
    报一串「'xxx' 不是内部或外部命令」——看起来像路径写错了，
    实际是行尾的问题。
    """
    raw = open(os.path.join(serve.ROOT, name), "rb").read()

    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), (
        f"{name} 里存在只有 LF 的行尾，cmd.exe 会解析错乱"
    )


@pytest.mark.parametrize("name", BAT_FILES)
def test_launchers_contain_no_multibyte_characters(name):
    """批处理里不能出现任何非 ASCII 字节。

    ``chcp 65001`` 下 cmd.exe 读批处理会出现多字节字符错位，而且**不是每行都
    出问题**：同一份文件里别的中文行显示正常，偏偏某一行被当成命令去执行
    （实测报 ``'��停止系统.bat」。' is not recognized ...``）。
    这种「部分正常」最难查，所以干脆把中文全部交给 Python 输出，
    批处理只保留 ASCII 的调度逻辑。
    """
    raw = open(os.path.join(serve.ROOT, name), "rb").read()

    offenders = [(index, byte) for index, byte in enumerate(raw) if byte > 127]
    assert not offenders, (
        f"{name} 里有非 ASCII 字节（位置 {offenders[:3]}）；"
        "中文提示请放到 tools/serve.py 里输出"
    )


# ---------------------------------------------------------------------------
# 状态文件的位置
# ---------------------------------------------------------------------------


def test_state_files_default_to_outside_the_project(isolate_state_files):
    """日志与 PID 文件默认必须在项目之外。

    放在项目里会被 Flask 的自动重载器监视到（run.py 是开着 DEBUG 的，
    重载器会遍历整个项目树）：

        启动 → 写日志 → 日志文件变动 → 触发重启 → 再写日志 → ……

    这不是「多几次重启」而已，是死循环。
    """
    log_path, pid_path = isolate_state_files

    assert serve.TMP == tempfile.gettempdir()
    root = os.path.abspath(serve.ROOT).rstrip("\\/") + os.sep

    for path in (log_path, pid_path):
        absolute = os.path.abspath(path)
        assert not absolute.startswith(root), f"{absolute} 落在项目目录里，会触发重启死循环"
        assert absolute.startswith(os.path.abspath(serve.TMP))
