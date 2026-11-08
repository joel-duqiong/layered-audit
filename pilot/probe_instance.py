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

import project


def source_container_name() -> str:
    """被克隆的源容器 —— 项目绑定里声明（app_container），不写死。"""
    return project.path("app_container", "jiuyan-backend")


def probe_default_env() -> dict[str, str]:
    """探针容器**默认**注入的环境变量 —— 项目绑定里声明。

    {name} 会被替换成探针实例名（各探针按实例名推导路径）。
    """
    raw = project.binding().get("probe_default_env") or {}
    return {k: str(v) for k, v in raw.items()}


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


def source_env(src_container: str = SRC_CONTAINER) -> tuple[str, str, dict[str, str]]:
    info = json.loads(docker("inspect", src_container))[0]
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
                 java_tool_options: str | None = None,
                 default_env: dict[str, str] | None = None,
                 drop_env: tuple[str, ...] = (),
                 src_container: str | None = None) -> None:
        self.name = name
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.env_overrides = env_overrides or {}
        self.java_tool_options = java_tool_options
        # 探针容器**默认**要注入哪些环境变量 —— 由项目声明（target.json）。
        # 之前这里写死了 FILE_UPLOAD_DIR：既是项目耦合，又让「属性绑定优先级」
        # 这类探针根本观察不到默认值（它总是被注入，赢的永远是它）。
        self.default_env = default_env if default_env is not None else probe_default_env()
        # 要从源容器继承的环境变量里剔掉的（同样是项目声明的）
        self.drop_env = tuple(drop_env)
        self.src_container = src_container or source_container_name()

    def __enter__(self) -> "ProbeInstance":
        image, network, env = source_env(self.src_container)
        docker("rm", "-f", self.name)
        args = ["run", "-d", "--name", self.name, "--network", network,
                "-p", f"127.0.0.1:{self.port}:{self.port}"]
        skip = ("OTEL_", "LANG", "LANGUAGE", "LC_ALL", "HOSTNAME")
        for k, v in env.items():
            if k.startswith(skip) or k in skip or k in self.drop_env:
                continue
            args += ["-e", f"{k}={v}"]
        for k, v in self.env_overrides.items():
            args += ["-e", f"{k}={v}"]
        for k, v in self.default_env.items():
            if k not in self.env_overrides:
                args += ["-e", f"{k}={v.replace('{name}', self.name)}"]
        args += ["-e", f"SERVER_PORT={self.port}"]
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
