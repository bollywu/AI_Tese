"""执行面沙箱（aicoverage/sandbox.py）单测：不依赖 docker daemon。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aicoverage.config import ProjectConfig  # noqa: E402
from aicoverage.sandbox import (  # noqa: E402
    DEFAULT_SANDBOX_IMAGE, DockerSandbox, HostSandbox, SandboxResult,
    get_sandbox, is_isolated, runtime_available,
)


def _mk_cfg(tmp_path: Path, **sb) -> ProjectConfig:
    """ProjectConfig.__new__ + 手工补字段（复刻仓库既有测试的构造方式）。"""
    src = tmp_path / "proj"
    src.mkdir(parents=True, exist_ok=True)
    cfg = ProjectConfig.__new__(ProjectConfig)
    cfg.config_path = src / "aicoverage.toml"
    cfg.name = "proj"
    cfg.display_name = "proj"
    cfg.source_path = src
    cfg.build_cmd = "make"
    cfg.binary = Path("app")
    cfg.test_dirname = "tests"
    cfg.test_timeout = 60
    for k, v in {
        "sandbox_enabled": False,
        "sandbox_runtime": "auto",
        "sandbox_image": DEFAULT_SANDBOX_IMAGE,
        "sandbox_network_build": True,
        "sandbox_network_test": False,
        "sandbox_memory": "2g",
        "sandbox_cpus": "2",
        "sandbox_pids_limit": 256,
        "sandbox_python": "python3",
        "sandbox_shell": "bash",
        "sandbox_collect_in_container": True,
        "sandbox_extra_args": [],
    }.items():
        setattr(cfg, k, v)
    for k, v in sb.items():
        setattr(cfg, k, v)
    return cfg


class TestHostSandbox:
    def test_runs_command_and_captures(self, tmp_path):
        res = HostSandbox().run("echo hello", cwd=tmp_path, timeout=30)
        assert isinstance(res, SandboxResult)
        assert res.rc == 0 and "hello" in res.log and res.backend == "host"

    def test_nonzero_rc_preserved(self, tmp_path):
        res = HostSandbox().run("exit 3", cwd=tmp_path, timeout=30)
        assert res.rc == 3

    def test_env_passed(self, tmp_path):
        res = HostSandbox().run("echo $AICOV_PROBE", cwd=tmp_path, timeout=30,
                                env={"AICOV_PROBE": "xyz"})
        assert "xyz" in res.log


class TestGetSandbox:
    def test_disabled_returns_host(self, tmp_path):
        cfg = _mk_cfg(tmp_path, sandbox_enabled=True)
        # runtime 探测被禁用模拟不可用也不影响：enabled=False 直接 Host
        assert get_sandbox(cfg).name == "host"

    def test_enabled_but_unavailable_degrades(self, tmp_path, monkeypatch):
        import aicoverage.sandbox as sb
        cfg = _mk_cfg(tmp_path, sandbox_enabled=True)
        monkeypatch.setattr(sb, "detect_runtime", lambda: None)
        s = sb.get_sandbox(cfg)
        assert s.name == "host" and not is_isolated(s)

    def test_enabled_with_runtime(self, tmp_path, monkeypatch):
        import aicoverage.sandbox as sb
        cfg = _mk_cfg(tmp_path, sandbox_enabled=True)
        monkeypatch.setattr(sb, "detect_runtime", lambda: "docker")
        s = sb.get_sandbox(cfg)
        assert isinstance(s, DockerSandbox) and s.name == "docker" and is_isolated(s)

    def test_runtime_pref_host_wins(self, tmp_path, monkeypatch):
        import aicoverage.sandbox as sb
        cfg = _mk_cfg(tmp_path, sandbox_enabled=True, sandbox_runtime="host")
        monkeypatch.setattr(sb, "detect_runtime", lambda: "docker")
        assert sb.get_sandbox(cfg).name == "host"


class TestDockerArgv:
    """argv 构造单测：不需要 docker daemon，直接检查命令行拼装。"""

    def _sb(self, tmp_path, **kw) -> DockerSandbox:
        return DockerSandbox(_mk_cfg(tmp_path, **kw), runtime="docker")

    def test_basic_shape_and_same_path_mount(self, tmp_path):
        cfg = _mk_cfg(tmp_path)
        s = DockerSandbox(cfg, runtime="docker")
        argv = s.build_argv(cwd=cfg.source_path, env={"AICOV_SRC": "/x"}, network=False)
        assert argv[:3] == ["docker", "run", "--rm"]
        assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
        # 同路径挂载（gcov 绝对路径约束）
        i = argv.index("-v")
        assert argv[i + 1] == f"{cfg.source_path}:{cfg.source_path}"
        # 命令经 stdin 传给 bash -s
        assert argv[-2:] == ["bash", "-s"]

    def test_network_flag(self, tmp_path):
        s = self._sb(tmp_path)
        argv = s.build_argv(cwd=tmp_path, env={}, network=True)
        assert argv[argv.index("--network") + 1] == "bridge"

    def test_resource_limits_and_user(self, tmp_path):
        s = self._sb(tmp_path)
        argv = s.build_argv(cwd=tmp_path, env={}, network=False)
        assert argv[argv.index("--memory") + 1] == "2g"
        assert argv[argv.index("--cpus") + 1] == "2"
        assert argv[argv.index("--pids-limit") + 1] == "256"
        if hasattr(__import__("os"), "getuid"):
            assert argv[argv.index("--user") + 1] == \
                f"{__import__('os').getuid()}:{__import__('os').getgid()}"

    def test_aicov_env_passthrough_and_home(self, tmp_path):
        s = self._sb(tmp_path)
        env = {"AICOV_SRC": "/p", "AICOV_RUN_DIR": "/p/.aicoverage/runs/LOOP_1",
               "SECRET": "no", "PATH": "/usr/bin"}
        argv = s.build_argv(cwd=tmp_path, env=env, network=False)
        joined = " ".join(argv)
        assert "-e AICOV_SRC=/p" in joined
        assert "-e AICOV_RUN_DIR=/p/.aicoverage/runs/LOOP_1" in joined
        assert "-e PATH=/usr/bin" in joined
        assert "SECRET=no" not in joined          # 非 passthrough 键不透传
        assert "-e HOME=/tmp" in joined           # 非 root 映射后可写 HOME

    def test_extra_args_appended(self, tmp_path):
        s = self._sb(tmp_path, sandbox_extra_args=["--security-opt", "no-new-privileges"])
        argv = s.build_argv(cwd=tmp_path, env={}, network=False)
        assert "--security-opt" in argv and "no-new-privileges" in argv


class TestCollectCommand:
    def test_collect_command_quotes_and_paths(self, tmp_path):
        from aicoverage.sandbox import sandbox_collect_command
        cfg = _mk_cfg(tmp_path)
        out = tmp_path / "iter_1" / "coverage.json"
        cmd = sandbox_collect_command(cfg, out)
        assert cmd.startswith("python3 -c ")
        assert "collect_coverage" in cmd
        assert str(cfg.config_path) in cmd and str(out) in cmd


class TestRuntimeAvailable:
    def test_missing_binary_is_unavailable(self, monkeypatch):
        import aicoverage.sandbox as sb
        monkeypatch.setattr(sb.shutil, "which", lambda name: None)
        sb._runtime_cache.clear()
        assert sb.runtime_available("docker") is False

    def test_daemon_unreachable_is_unavailable(self, monkeypatch):
        import aicoverage.sandbox as sb

        class P:
            returncode = 1
            stdout = ""

        monkeypatch.setattr(sb.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(sb.subprocess, "run", lambda *a, **k: P())
        sb._runtime_cache.clear()
        assert sb.runtime_available("docker") is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
