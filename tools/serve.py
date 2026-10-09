"""把本地开发服务器挂到后台跑，关掉终端也不会停。

和直接跑 ``run.py`` 的区别：

1. **要关掉 DEBUG。** ``DevConfig`` 里 ``DEBUG = True``，Flask 会同时开启调试器
   （带 PIN 的交互式控制台）和自动重载。作为「挂在后台长期跑」的进程，这两样
   都不合适。
2. **自动重载会监视整个项目树**，``.workbuddy/`` 里任何文件被改动——写一条工作
   记录、生成一个临时文件——都会触发重启。日志如果也写在项目里，就会变成
   「重启→写日志→日志变动→再重启」的死循环。
3. **只有一个进程。** 开着自动重载时是「监视父进程 + 干活的子进程」两个，
   要停就得按整棵进程树杀；关掉之后按 PID 杀一个就够了。
4. **不占用终端。** 用 DETACHED_PROCESS 起，关掉这个终端窗口它也照跑。

日志和 PID 文件都放在系统临时目录（项目之外），既不会被重载器监视，
也不污染仓库。

用法::

    python tools/serve.py start    # 后台启动，就绪后打印地址
    python tools/serve.py status    # 看还在不在
    python tools/serve.py stop      # 停掉

写代码的时候还是用 ``python run.py``（那条路开着自动重载，改完存盘就生效）；
这个脚本是给「我只是想打开页面看看」准备的。
"""

import ctypes
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

def _find_root(start):
    """从 start 往上找项目根目录。

    不写死 ``os.pardir`` 的层数：这个脚本在项目里的位置换一次，
    层数就得跟着改一次，改漏了的表现是子进程起来后报
    ``ModuleNotFoundError: No module named 'app'``——而它是以后台方式跑的，
    不主动去看日志根本发现不了。改成按标志文件（run.py + app/）向上找。
    """
    current = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(current, "run.py")) and os.path.isdir(
            os.path.join(current, "app")
        ):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            raise RuntimeError("向上找不到项目根目录（应同时包含 run.py 与 app/）")
        current = parent


ROOT = _find_root(os.path.dirname(os.path.abspath(__file__)))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "5000"))

TMP = tempfile.gettempdir()
LOG_PATH = os.path.join(TMP, "campus-dev-server.log")
PID_PATH = os.path.join(TMP, "campus-dev-server.json")

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


def port_open(host=HOST, port=PORT, timeout=1.0):
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def process_alive(pid):
    """Windows 上判断进程是否还活着。

    不能用 ``os.kill(pid, 0)``——Windows 下 os.kill 只认 CTRL_C_EVENT /
    CTRL_BREAK_EVENT，传别的信号会被当成 TerminateProcess，也就是**探活会把
    进程杀掉**。
    """
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def read_state():
    if not os.path.exists(PID_PATH):
        return {}
    try:
        with open(PID_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _spawn(flags):
    """把服务进程拉起来，日志直接追加到文件。"""
    with open(LOG_PATH, "a", encoding="utf-8") as log:
        return subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "_serve"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=flags,
            close_fds=True,
        )


def serve_forever():
    """真正的服务进程：关掉调试器与自动重载，日志追加到临时目录。"""
    os.chdir(ROOT)
    sys.path.insert(0, ROOT)

    with open(LOG_PATH, "a", encoding="utf-8", buffering=1) as log:
        sys.stdout = log
        sys.stderr = log
        print(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 {HOST}:{PORT} ===")

        from app import create_app

        application = create_app()
        application.run(
            host=HOST,
            port=PORT,
            debug=False,        # 别留着一个带交互式调试控制台的进程在后台
            use_reloader=False,  # 单进程，便于停止；也避免被 .workbuddy/ 扰动触发重启
            threaded=True,
        )


def start():
    state = read_state()
    pid = state.get("pid")
    if pid and process_alive(pid):
        print(f"已经在跑了：PID {pid}  http://{HOST}:{PORT}")
        print(f"日志：{LOG_PATH}")
        return 0
    if port_open():
        print(f"端口 {PORT} 已被别的进程占用，先确认它是不是你之前开的：")
        print(f'  netstat -ano | findstr :{PORT}')
        return 1

    # DETACHED_PROCESS：不继承当前控制台。
    # CREATE_NEW_PROCESS_GROUP：不受发给当前控制台的 Ctrl+C 影响。
    # CREATE_BREAKAWAY_FROM_JOB：如果调用方被放在 Windows 的「作业对象」里
    #   （自动化的进程清理常常就是这么实现的），带着这个标志可以让子进程
    #   脱离作业，父进程被回收时不会连带被终止。
    #   作业对象没有开启 breakaway 权限时会直接报「拒绝访问」，
    #   所以准备好降级重试——降级后能不能活下来就不一定了。
    base_flags = 0x00000008 | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        child = _spawn(base_flags | 0x01000000)
    except OSError:
        child = _spawn(base_flags)

    with open(PID_PATH, "w", encoding="utf-8") as handle:
        json.dump({"pid": child.pid, "port": PORT, "started_at": time.time()}, handle)

    # 等它真的开始监听，而不是「启动命令返回了 0」就算成功
    for _ in range(60):
        if child.poll() is not None:
            print(f"进程启动后立刻退出了（exit={child.returncode}），日志末尾：")
            print(_tail(LOG_PATH))
            return 1
        if port_open():
            print(f"已启动：PID {child.pid}")
            print(f"地址：http://{HOST}:{PORT}")
            print(f"日志：{LOG_PATH}")
            return 0
        time.sleep(0.5)

    print("等了一分钟还没监听端口，日志末尾：")
    print(_tail(LOG_PATH))
    return 1


def status():
    state = read_state()
    pid = state.get("pid")
    alive = bool(pid) and process_alive(pid)
    listening = port_open()
    print(f"PID {pid or '（无记录）'}  进程存活：{'是' if alive else '否'}  "
          f"端口 {PORT} 监听中：{'是' if listening else '否'}")
    if listening:
        print(f"地址：http://{HOST}:{PORT}")
    print(f"日志：{LOG_PATH}")
    return 0 if (alive and listening) else 1


def stop():
    state = read_state()
    pid = state.get("pid")
    if not pid:
        print("没有记录到进程；如果端口还占着，用 netstat 找一下：")
        print(f'  netstat -ano | findstr :{PORT}')
        return 1

    if not process_alive(pid):
        print(f"PID {pid} 已经不在了，清理记录")
        os.remove(PID_PATH)
        return 0

    # 不能用 text=True + encoding="utf-8"：taskkill 的输出是控制台代码页
    # （简体中文 Windows 上是 GBK），强按 UTF-8 解会在读取线程里抛
    # UnicodeDecodeError。虽然不影响功能，但会打出一堆吓人的调用栈。
    result = subprocess.run(
        ["taskkill", "/F", "/PID", str(pid)], capture_output=True
    )
    message = (result.stdout + result.stderr).decode("mbcs", errors="replace").strip()

    for _ in range(20):
        if not process_alive(pid):
            break
        time.sleep(0.25)

    if process_alive(pid):
        print(f"PID {pid} 没能停掉，手动试：taskkill /F /PID {pid}")
    else:
        print(f"已停止 PID {pid}")
    if message:
        print(f"  taskkill: {message.splitlines()[0]}")

    if os.path.exists(PID_PATH):
        os.remove(PID_PATH)
    return 0


def _tail(path, lines=15):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return "".join(handle.readlines()[-lines:])
    except OSError:
        return "（读不到日志）"


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "_serve":
        serve_forever()
        return
    handlers = {"start": start, "stop": stop, "status": status}
    if action not in handlers:
        sys.exit(__doc__)
    sys.exit(handlers[action]())


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
