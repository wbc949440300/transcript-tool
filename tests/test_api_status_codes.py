"""失败响应的 HTTP 状态码要一致（2026-10-31 实测发现三处不一致）。

- `/transcript/image/ocr/doTask` 失败时曾返回 HTTP 200 + body.code=500
- `/history/delete` 删不存在的记录曾返回 HTTP 200（现在与 /history/detail 一致走 404）
"""
import pytest
from fastapi.testclient import TestClient

from app.history import history_store
from app.models import TaskDetail, TaskStatus

TOKEN = "test-token"


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app, base_url="http://127.0.0.1:8000")


def _auth() -> dict:
    return {"X-API-Token": TOKEN}


class _Boom:
    def open(self, req, timeout=None):
        raise RuntimeError("抓图失败：模拟网络错误")


def test_ocr_url_failure_returns_500(client, monkeypatch):
    from app import utils

    # 端点里是 "from .utils import http_get_bytes"（局部导入，且 2026-11-03 起取图
    # 统一走这个带退避重试的封装），所以 patch api.http_opener 打不到它 —— 会真的
    # 去请求外网（实测拿到 "HTTP Error 404"）。这里直接让取图函数抛错。
    def _boom(*a, **k):
        raise RuntimeError("抓图失败：模拟网络错误")

    monkeypatch.setattr(utils, "http_get_bytes", _boom)
    r = client.post("/prod-api/transcript/image/ocr/doTask",
                    json={"sourceUrl": "https://example.com/x.png"}, headers=_auth())
    assert r.status_code == 500, "OCR 失败要走 HTTP 5xx，不能只改 body 里的 code"
    body = r.json()
    assert body["code"] == 500
    assert "抓图失败" in body["msg"]
    # body 形状保持不变：页面照旧能读到 data.text / data.status
    assert (body.get("data") or {}).get("status") == "FAILURE"
    assert (body.get("data") or {}).get("text") == ""


def test_ocr_url_bad_input_still_400(client):
    r = client.post("/prod-api/transcript/image/ocr/doTask",
                    json={"sourceUrl": "不是链接"}, headers=_auth())
    assert r.status_code == 400


def test_history_delete_missing_returns_404(client):
    history_store.clear()
    r = client.delete("/prod-api/history/delete?taskId=nope", headers=_auth())
    assert r.status_code == 404
    assert r.json()["msg"] == "记录不存在"


def test_history_delete_existing_returns_200(client):
    history_store.clear()
    task = TaskDetail(taskId="del_ok_1", taskType="transcript",
                      status=TaskStatus.SUCCESS, title="测试")
    task.createTime = "2026-10-31 10:00:00"
    history_store.save(task)

    r = client.delete("/prod-api/history/delete?taskId=del_ok_1", headers=_auth())
    assert r.status_code == 200
    assert r.json()["data"]["deleted"] is True


def test_history_delete_without_id_still_400(client):
    r = client.delete("/prod-api/history/delete", headers=_auth())
    assert r.status_code == 400


def test_validation_error_uses_project_envelope(client):
    """Pydantic 校验失败也必须走 {code,msg,data}，不能漏 FastAPI 默认的 {detail:[...]}。

    2026-11-02 实测：POST /ai/correct 缺 taskId 时返回 422 +
    {"detail":[{"type":"missing","loc":["body","taskId"],"msg":"Field required"}]}，
    与全站契约不一致，前端按 code/msg 解析会拿到 undefined。
    """
    r = client.post("/prod-api/ai/correct", json={}, headers=_auth())
    assert r.status_code == 400, "校验失败按参数错误统一走 400，不再是 422"
    body = r.json()
    assert body["code"] == 500
    assert "缺少请求参数" in body["msg"]
    assert "taskId" in body["msg"]
    assert "detail" not in body, "不能把 FastAPI 默认的 detail 数组漏给前端"
    assert "data" in body


def test_validation_error_bad_type_reports_field(client):
    """类型不对也要给出中文字段名，而不是英文的 type/loc 结构。"""
    r = client.get("/prod-api/ext/tasks?limit=abc", headers=_auth())
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == 500
    assert "limit" in body["msg"]
    assert "detail" not in body


def test_validation_error_malformed_json(client):
    """请求体不是合法 JSON 时也不能把 loc 里的字符偏移量当字段名展示。"""
    r = client.post("/prod-api/ai/correct", content=b'{"taskId": bad',
                    headers={**_auth(), "Content-Type": "application/json"})
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == 500
    assert "JSON" in body["msg"]
    assert "参数格式不正确: 11" not in body["msg"], "别把字符偏移量当字段名"
    assert "detail" not in body


def test_export_missing_task_returns_404(client):
    """导出不存在的任务走 404，与 /history/detail 等兄弟接口一致（此前一律 400）。"""
    r = client.get("/prod-api/ai-face/exportTask?taskId=nope&format=txt", headers=_auth())
    assert r.status_code == 404
    assert "任务不存在" in r.json()["msg"]


def test_export_bad_format_still_400(client):
    """格式不支持仍是 400 —— 不能被上面的 404 改动误伤。"""
    history_store.clear()
    task = TaskDetail(taskId="exp_bad_fmt", taskType="transcript",
                      status=TaskStatus.SUCCESS, title="导出测试")
    task.createTime = "2026-11-02 10:00:00"
    task.textContent = "正文"
    history_store.save(task)

    r = client.get("/prod-api/ai-face/exportTask?taskId=exp_bad_fmt&format=nope",
                   headers=_auth())
    assert r.status_code == 400
    assert "不支持的导出格式" in r.json()["msg"]