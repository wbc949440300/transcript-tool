"""启动前运行时准备：跨平台安全（云端 Linux 上 os.add_dll_directory 不存在也不能炸）。"""
import os

from app.env import prepare_runtime_env


def test_picks_bundled_ffmpeg(tmp_path, monkeypatch):
    d = tmp_path / "tools" / "ffmpeg" / "bin"
    d.mkdir(parents=True)
    monkeypatch.setenv("PATH", "/usr/bin")
    info = prepare_runtime_env(root=tmp_path)
    assert info["ffmpeg_dir"] == str(d)
    assert os.environ["PATH"].startswith(str(d) + os.pathsep)


def test_survives_missing_add_dll_directory(tmp_path, monkeypatch):
    """模拟非 Windows：没有 os.add_dll_directory 也必须能跑完。"""
    (tmp_path / "tools" / "ffmpeg" / "bin").mkdir(parents=True)
    monkeypatch.delattr(os, "add_dll_directory", raising=False)
    info = prepare_runtime_env(root=tmp_path)
    assert info["dll_dir_added"] is False
    assert info["ffmpeg_dir"]                     # PATH 仍然被前置


def test_no_tools_dir_is_fine(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    info = prepare_runtime_env(root=tmp_path)
    assert info["ffmpeg_dir"] == ""
    assert info["hf_endpoint"].startswith("http")   # 默认走 hf-mirror


def test_does_not_override_user_hf_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_ENDPOINT", "https://my-mirror.example")
    info = prepare_runtime_env(root=tmp_path)
    assert info["hf_endpoint"] == "https://my-mirror.example"