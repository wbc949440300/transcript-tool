"""监听目录支持图片：同一批图片合成一条 OCR 任务，媒体文件仍走转写。"""
import pytest

from app import image_import, watch_folder
from app.models import TaskDetail, TaskStatus
from app.task_store import store


@pytest.fixture
def watch_dir(tmp_path, monkeypatch):
    d = tmp_path / "watch"
    d.mkdir()
    monkeypatch.chdir(tmp_path)                 # tmp_uploads 是相对路径
    monkeypatch.setenv("TRANSCRIPT_WATCH_IMAGES", "1")
    watch_folder._seen.clear()                  # 模块级记忆，测试间要清
    return d


def _scan_twice(d, model="tiny", move=False):
    watch_folder._scan_once(d, model, move)     # 第一轮：只记指纹（等文件写完）
    watch_folder._scan_once(d, model, move)     # 第二轮：稳定 → 入列


def test_is_image_ext():
    assert image_import.is_image_ext("a.JPG")
    assert image_import.is_image_ext("b.png")
    assert image_import.is_image_ext("c.webp")
    assert not image_import.is_image_ext("d.mp4")
    assert not image_import.is_image_ext("e.txt")


def test_batch_images_become_one_task(watch_dir, monkeypatch):
    captured = {}

    def fake_start(task, paths, names, **kw):
        captured.update({"task": task, "paths": list(paths), "names": list(names)})

    monkeypatch.setattr(watch_folder, "start_image_task", fake_start)
    for i in range(3):
        (watch_dir / f"图集_{i + 1}.jpg").write_bytes(b"x" * (10 + i))

    _scan_twice(watch_dir)

    assert captured, "应创建图片任务"
    assert len(captured["paths"]) == 3, "同一批图片应合成一个任务"
    assert captured["task"].taskType == "image-ocr"
    assert captured["task"].platform == "图片OCR"
    assert "3 张图" in captured["task"].title
    assert all("tmp_uploads" in str(p) for p in captured["paths"])


def test_media_files_still_transcribe(watch_dir, monkeypatch):
    captured = {}
    monkeypatch.setattr(watch_folder, "start_file_task",
                        lambda task, path, model, name, ocr_frames=False, **kw: captured.update({"name": name}))
    monkeypatch.setattr(watch_folder, "start_image_task", lambda *a, **k: captured.update({"img": True}))

    (watch_dir / "voice.mp3").write_bytes(b"x" * 20)
    _scan_twice(watch_dir)

    assert captured.get("name") == "voice.mp3"
    assert "img" not in captured


def test_images_can_be_disabled(watch_dir, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_WATCH_IMAGES", "0")
    called = []
    monkeypatch.setattr(watch_folder, "start_image_task", lambda *a, **k: called.append(1))

    (watch_dir / "a.jpg").write_bytes(b"x" * 10)
    _scan_twice(watch_dir)

    assert called == []


def test_unknown_extensions_ignored(watch_dir, monkeypatch):
    called = []
    monkeypatch.setattr(watch_folder, "start_image_task", lambda *a, **k: called.append(1))
    monkeypatch.setattr(watch_folder, "start_file_task", lambda *a, **k: called.append(1))

    (watch_dir / "notes.txt").write_bytes(b"x" * 10)
    _scan_twice(watch_dir)

    assert called == []


def test_image_task_joins_text_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(image_import, "ocr_image", lambda data: "识别到的一行字")

    p1 = tmp_path / "1.jpg"
    p2 = tmp_path / "2.jpg"
    p1.write_bytes(b"a")
    p2.write_bytes(b"b")
    task = TaskDetail(taskId="img_task_1", taskType="image-ocr", status=TaskStatus.PENDING,
                      title="测试图集", platform="图片OCR")
    store.create(task)

    image_import._run_image_task(task, [p1, p2], ["1.jpg", "2.jpg"])

    assert task.status == TaskStatus.SUCCESS
    assert task.textContent == "识别到的一行字\n\n识别到的一行字"
    assert not p1.exists() and not p2.exists(), "临时副本应被清理"


def test_image_task_fails_when_no_text(tmp_path, monkeypatch):
    monkeypatch.setattr(image_import, "ocr_image", lambda data: "")
    p = tmp_path / "empty.jpg"
    p.write_bytes(b"a")
    task = TaskDetail(taskId="img_task_2", taskType="image-ocr", status=TaskStatus.PENDING,
                      title="无字图", platform="图片OCR")
    store.create(task)

    image_import._run_image_task(task, [p], ["empty.jpg"])

    assert task.status == TaskStatus.FAILURE
    assert "没识别到文字" in (task.errorMessage or "")