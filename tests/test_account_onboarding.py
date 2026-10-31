"""账号接入：每号每日上限 + 强制指定账号（巡检用）+ 批量导入的解析校验。"""
import importlib.util
import json
import pathlib

import pytest

from app import utils

_SPEC = importlib.util.spec_from_file_location(
    "import_accounts",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "import_accounts.py")
imp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(imp)


@pytest.fixture
def pool(tmp_path, monkeypatch):
    """一个只有 2 个号的临时池子。

    注意：`utils.COOKIE_DIR` 是**导入时**读环境变量定下的，所以改 env 没用——
    必须直接 monkeypatch 模块属性（conftest 建的那个 cookies 目录留给其它用例）。
    """
    monkeypatch.setattr(utils, "COOKIE_DIR", tmp_path)
    monkeypatch.setenv("TRANSCRIPT_COOKIE_DIR", str(tmp_path))
    d = tmp_path / "douyin"
    d.mkdir()
    for i in (1, 2):
        (d / f"account_{i}.txt").write_text(
            "# Netscape HTTP Cookie File\n"
            f".douyin.com\tTRUE\t/\tTRUE\t0\tsessionid\tFAKE{i}\n", encoding="utf-8")
    return d


def test_daily_cap_balances_across_accounts(pool, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_ACCOUNT_DAILY_MAX", "2")
    picks = [utils.account_key(utils.pick_account("douyin")) for _ in range(4)]
    # 上限 2：前 4 次应当正好每个号 2 次（均衡），不会出现某个号被连挑 3 次以上
    assert picks.count("account_1") == 2 and picks.count("account_2") == 2


def test_daily_cap_falls_back_when_all_capped(pool, monkeypatch, caplog):
    """全部到上限时不能停摆：临时放宽并给出警告（10 分钟最多报一次）。"""
    monkeypatch.setenv("TRANSCRIPT_ACCOUNT_DAILY_MAX", "1")
    first = utils.account_key(utils.pick_account("douyin"))
    second = utils.account_key(utils.pick_account("douyin"))
    assert {first, second} == {"account_1", "account_2"}
    third = utils.pick_account("douyin")                 # 两个号都已到上限
    assert third is not None                              # 仍然返回，不抛异常


def test_snapshot_reports_cap(pool, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_ACCOUNT_DAILY_MAX", "1")
    utils.pick_account("douyin")
    snap = {s["name"]: s for s in utils.account_pool_snapshot("douyin")}
    assert snap["account_1"]["usedToday"] == 1
    assert snap["account_1"]["dailyMax"] == 1
    assert snap["account_1"]["capped"] is True
    assert snap["account_2"]["capped"] is False


def test_force_account_hook(pool, monkeypatch):
    """TRANSCRIPT_ACCOUNT_FORCE：巡检脚本靠它把请求固定到某个号上。"""
    target = str(pool / "account_2.txt")
    monkeypatch.setenv("TRANSCRIPT_ACCOUNT_FORCE", target)
    assert utils.pick_account("douyin") == target
    monkeypatch.delenv("TRANSCRIPT_ACCOUNT_FORCE", raising=False)
    assert utils.pick_account("douyin") in (str(pool / "account_1.txt"), target)


def test_import_parses_netscape_file(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("# Netscape HTTP Cookie File\n"
                 ".douyin.com\tTRUE\t/\tTRUE\t0\tsessionid\tX\n"
                 "garbage line\n", encoding="utf-8")
    lines, warns = imp._parse_source(f, "douyin")
    assert len(lines) == 1 and "sessionid" in lines[0]
    assert warns == []


def test_import_parses_cookie_string(tmp_path):
    f = tmp_path / "b.txt"
    f.write_text("sessionid=ABC; odin_tt=DEF\n", encoding="utf-8")
    lines, warns = imp._parse_source(f, "douyin")
    assert len(lines) == 2
    assert all(ln.startswith(".douyin.com\t") for ln in lines)
    assert any("cookie 串" in w for w in warns)


def test_import_rejects_junk(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text("这不是 cookie\n", encoding="utf-8")
    lines, warns = imp._parse_source(f, "douyin")
    assert lines == [] and warns


def test_import_warns_on_wrong_platform(tmp_path):
    f = tmp_path / "d.txt"
    f.write_text(".bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tX\n", encoding="utf-8")
    lines, warns = imp._parse_source(f, "douyin")
    assert lines and any("可能拿错平台" in w for w in warns)


def test_import_names_missing_key_field(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text(".douyin.com\tTRUE\t/\tTRUE\t0\tsomethingelse\tX\n", encoding="utf-8")
    lines, warns = imp._parse_source(f, "douyin")
    assert lines and any("缺少关键字段" in w for w in warns)


def test_import_next_index(tmp_path):
    (tmp_path / "account_1.txt").write_text("x", encoding="utf-8")
    (tmp_path / "account_3.txt").write_text("x", encoding="utf-8")
    assert imp._next_index(tmp_path) == 2