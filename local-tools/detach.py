#!/usr/bin/env python3
"""把一个命令完全脱离当前进程组/会话地启动（纯标准库）。

为什么需要它：从智能体沙箱里用 `nohup cmd & disown` 启动的进程，会在该条
命令结束后随进程组一起被回收。这里用「双重 fork + setsid」建立独立会话，
新进程不再属于原进程组，因此不会被连带杀掉。

用法：
    python3 local-tools/detach.py --pid logs/x.pid --log logs/x.log -- \
        /usr/bin/python3 some_server.py --port 1234
"""

from __future__ import annotations

import argparse
import os
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser(description="detached process launcher")
    ap.add_argument("--pid", required=True, help="pidfile 路径")
    ap.add_argument("--log", required=True, help="stdout/stderr 重定向目标")
    ap.add_argument("--wait-port", default="", help="可选 host:port，等待其可连接后再返回")
    ap.add_argument("cmd", nargs=argparse.REMAINDER, help="-- 之后的命令")
    args = ap.parse_args()

    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("错误：没有给出要执行的命令", file=sys.stderr)
        return 2

    pid_path = os.path.abspath(args.pid)
    log_path = os.path.abspath(args.log)
    os.makedirs(os.path.dirname(pid_path), exist_ok=True)

    # 第一层 fork：父进程留着等中间进程退出，保证命令行返回时 pidfile 已写好
    first = os.fork()
    if first > 0:
        os.waitpid(first, 0)
        try:
            with open(pid_path) as fh:
                child_pid = int(fh.read().strip())
            print(f"已启动，PID {child_pid}（脱离当前会话，不受进程组回收影响）")
        except Exception:
            print("已启动（未能读取 pidfile）", file=sys.stderr)
        return 0

    # 中间进程：建立新会话，成为会话首进程
    os.setsid()

    # 第二层 fork：确保不再是会话首进程，且父进程立刻退出后由 init 收养
    second = os.fork()
    if second > 0:
        with open(pid_path, "w") as fh:
            fh.write(str(second))
        os._exit(0)

    # 孙进程：真正干活的那个，重定向 stdio 后 exec
    try:
        null_in = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null_in, 0)
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
    except Exception:
        pass

    os.execvp(cmd[0], cmd)
    os._exit(127)


if __name__ == "__main__":
    raise SystemExit(main())
