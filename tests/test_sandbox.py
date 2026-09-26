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
        cfg = _mk_cfg(tmp_path)          # 默认 sandbox_enabled=False
        # runtime 探测被禁用模拟不可用也不影响：enabled=False 直接 Host
        assert get_sandbox(cfg).name == "host"

    def test_enabled_but_unavailable_degrades(self, tmp_path, monkeypatch):
        import aicoverage.sandbox as sb
        cfg = _mk_cfg(tmp_path, sandbox_enabled=True)
        monkeypatch.setattr(sb, "detect_runtime", lambda *a, **k: None)
        s = sb.get_sandbox(cfg)
        assert s.name == "host" and not is_isolated(s)

    def test_enabled_with_runtime(self, tmp_path, monkeypatch):
        import aicoverage.sandbox as sb
        cfg = _mk_cfg(tmp_path, sandbox_enabled=True)
        monkeypatch.setattr(sb, "detect_runtime", lambda *a, **k: "docker")
        s = sb.get_sandbox(cfg)
        assert isinstance(s, DockerSandbox) and s.name == "docker" and is_isolated(s)

    def test_runtime_pref_host_wins(self, tmp_path, monkeypatch):
        import aicoverage.sandbox as sb
        cfg = _mk_cfg(tmp_path, sandbox_enabled=True, sandbox_runtime="host")
        monkeypatch.setattr(sb, "detect_runtime", lambda *a, **k: "docker")
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

    def test_disk_cache_hit_avoids_probe(self, tmp_path, monkeypatch):
        import json
        import aicoverage.sandbox as sb
        cache = tmp_path / ".aicoverage"
        cache.mkdir()
        (cache / "sandbox_runtime_cache.json").write_text(
            json.dumps({"docker": {"ok": True, "ts": __import__("time").time()}}))
        sb._runtime_cache.clear()

        def _boom(*a, **k):        # 磁盘缓存命中则不会真的探测
            raise AssertionError("不应触发探测")

        monkeypatch.setattr(sb.shutil, "which", _boom)
        assert sb.runtime_available("docker", cache) is True


class TestAgentShims:
    """P4：agent Bash 的编译类命令经 PATH shim 透明转发进容器。"""

    def test_disabled_returns_none(self, tmp_path):
        import aicoverage.sandbox as sb
        cfg = _mk_cfg(tmp_path, sandbox_enabled=False)
        assert sb.ensure_agent_shims(cfg) is None

    def test_opt_out_returns_none(self, tmp_path):
        import aicoverage.sandbox as sb
        cfg = _mk_cfg(tmp_path, sandbox_enabled=True, sandbox_agent_shims=False)
        assert sb.ensure_agent_shims(cfg) is None

    def test_shims_generated_and_executable(self, tmp_path):
        import aicoverage.sandbox as sb
        cfg = _mk_cfg(tmp_path, sandbox_enabled=True)
        shims = sb.ensure_agent_shims(cfg)
        assert shims is not None and shims.is_dir()
        for name in ("gcc", "make", "cmake"):
            script = shims / name
            assert script.exists()
            assert script.stat().st_mode & 0o111, f"{name} 应可执行"
            body = script.read_text(encoding="utf-8")
            assert "sandbox_shim" in body and name in body
        # 自定义命令表生效
        cfg2 = _mk_cfg(tmp_path / "p2", sandbox_enabled=True,
                       sandbox_agent_shim_commands=["mycc"])
        sb._shims_cache.clear()
        shims2 = sb.ensure_agent_shims(cfg2)
        assert (shims2 / "mycc").exists() and not (shims2 / "gcc").exists()

    def test_interactive_argv_uses_env_cmd_not_stdin(self, tmp_path):
        """run_pipe 模式：命令经 AICOV_SHIM_CMD 环境变量传入，stdin 留给真实命令。"""
        s = DockerSandbox(_mk_cfg(tmp_path), runtime="docker")
        argv = s.build_argv(cwd=tmp_path, env={}, network=False, cmd='gcc -c "a b.c"')
        joined = " ".join(argv)
        assert '-e AICOV_SHIM_CMD=gcc -c "a b.c"' in joined
        assert argv[-3:] == ["bash", "-c", 'eval "$AICOV_SHIM_CMD"']
        assert argv[-2:] != ["bash", "-s"]

    def test_runner_env_injects_shim_path(self, tmp_path, monkeypatch):
        """AgentRunner._build_env：沙箱启用时 PATH 前置 shim 目录 + PYTHONPATH。"""
        from aicoverage.runner import AgentRunner
        cfg = _mk_cfg(tmp_path, sandbox_enabled=True)
        shims = str(cfg.workspace / "shims")
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.delenv("PYTHONPATH", raising=False)
        import aicoverage.sandbox as sb
        monkeypatch.setattr(sb, "ensure_agent_shims", lambda c: __import__(
            "pathlib").Path(shims))
        env = AgentRunner(cfg)._build_env()
        assert env["PATH"].startswith(shims + ":"), "shim 目录必须前置 PATH"
        # PYTHONPATH 指向 aicoverage 包根（shim 脚本要能 import aicoverage）
        import aicoverage as _pkg
        assert Path(env["PYTHONPATH"]) == Path(_pkg.__file__).resolve().parent.parent

    def test_runner_env_untouched_when_disabled(self, tmp_path, monkeypatch):
        from aicoverage.runner import AgentRunner
        cfg = _mk_cfg(tmp_path, sandbox_enabled=False)
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.delenv("PYTHONPATH", raising=False)
        env = AgentRunner(cfg)._build_env()
        assert env["PATH"] == "/usr/bin"

    def test_shim_falls_back_to_host_when_not_isolated(self, tmp_path, monkeypatch):
        """沙箱不可用时，shim 入口 execvp 回退宿主机原样执行。"""
        import aicoverage.sandbox_shim as shim

        ran: list[list[str]] = []

        def fake_execvp(name, argv):
            ran.append(list(argv))
            raise IndexError("模拟 execvp 不返回（真实场景进程已被替换）")

        monkeypatch.setattr(shim, "load_config",
                            lambda *a, **k: _mk_cfg(tmp_path, sandbox_enabled=True))
        monkeypatch.setattr(shim, "get_sandbox",
                            lambda cfg: __import__("aicoverage.sandbox",
                                                   fromlist=["HostSandbox"]).HostSandbox())
        monkeypatch.setattr(shim.os, "execvp", fake_execvp)
        with pytest.raises(IndexError):
            shim.main(["gcc", "-v"])
        assert ran and ran[0][:2] == ["gcc", "-v"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
