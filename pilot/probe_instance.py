"""临时探针实例：克隆正在跑的后端容器，换环境变量，用完拆掉。

为什么需要：有些语义只能在**真实 Spring 容器**里观察（配置绑定、切面、ORM 语句数），
而动不了用户正在跑的那个实例。克隆它 —— 镜像、网络、环境变量全部从 `docker inspect`
现场读，所以密码之类的不会进仓库；只覆盖端口、上传目录和本次要测的那几个变量。

测完 `docker rm -f`，不留痕迹。
"""
from __future__ import annotations

import json
import socket
import subprocess
import time
from typing import Any

import requests

SRC_CONTAINER = "jiuyan-backend"


def docker(*args: str) -> str:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"docker {args[0]} 失败: {proc.stderr.strip()[-300:]}")
    return proc.stdout


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def source_env() -> tuple[str, str, dict[str, str]]:
    info = json.loads(docker("inspect", SRC_CONTAINER))[0]
    image = info["Config"]["Image"]
    network = next(iter(info["NetworkSettings"]["Networks"]))
    env: dict[str, str] = {}
    for item in info["Config"].get("Env") or []:
        k, _, v = item.partition("=")
        env[k] = v
    return image, network, env


class ProbeInstance:
    """一个临时的后端实例。env_overrides 里放要改的环境变量。"""

    def __init__(self, name: str, env_overrides: dict[str, str] | None = None,
                 java_tool_options: str | None = None) -> None:
        self.name = name
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.env_overrides = env_overrides or {}
        self.java_tool_options = java_tool_options

    def __enter__(self) -> "ProbeInstance":
        image, network, env = source_env()
        docker("rm", "-f", self.name)
        args = ["run", "-d", "--name", self.name, "--network", network,
                "-p", f"127.0.0.1:{self.port}:{self.port}"]
        skip = ("OTEL_", "LANG", "LANGUAGE", "LC_ALL", "HOSTNAME")
        for k, v in env.items():
            if k.startswith(skip) or k in skip:
                continue
            args += ["-e", f"{k}={v}"]
        for k, v in self.env_overrides.items():
            args += ["-e", f"{k}={v}"]
        args += ["-e", f"SERVER_PORT={self.port}",
                 "-e", f"FILE_UPLOAD_DIR={self.env_overrides.get('FILE_UPLOAD_DIR', f'/tmp/{self.name}-uploads')}"]
        if self.java_tool_options:
            args += ["-e", f"JAVA_TOOL_OPTIONS={self.java_tool_options}"]
        args.append(image)
        docker(*args)
        for _ in range(90):
            try:
                if requests.get(f"{self.base}/api/health", timeout=2).status_code == 200:
                    return self
            except requests.RequestException:
                pass
            time.sleep(1)
        # __enter__ 抛异常时 __exit__ 不会被调用 —— 必须自己收尾，否则容器残留
        self.__exit__()
        raise RuntimeError(f"探针实例 {self.name} 没起来")

    def __exit__(self, *exc: Any) -> None:
        try:
            docker("rm", "-f", self.name)
        except RuntimeError:
            pass

    def logs(self) -> str:
        return docker("logs", self.name)

    def session(self) -> requests.Session:
        return requests.Session()
