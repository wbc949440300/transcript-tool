"""归档：启动补档必须包含【画面文字】段（历史 extra 里的 frameText）。"""
from app import archive
from app.history import history_store
from app.models import TaskDetail, TaskStatus


def test_backfill_includes_frame_text():
    history_store.clear()
    task = TaskDetail(taskId="arch_frame_1", taskType="transcript",
                      status=TaskStatus.SUCCESS, title="画面文字测试",
                      platform="抖音", textContent="口播正文")
    task.createTime = "2026-10-31 09:00:00"
    task.frameText = "画面里的字"
    history_store.save(task)

    n = archive.backfill()
    files = list(archive.archive_dir().glob("*画面文字测试*.txt"))
    assert n >= 1 and files, "补档应生成归档文件"

    body = files[0].read_text(encoding="utf-8-sig")
    assert "口播正文" in body
    assert "【画面文字】" in body
    assert "画面里的字" in body


def test_failed_task_is_not_archived():
    history_store.clear()
    task = TaskDetail(taskId="arch_fail_1", taskType="transcript",
                      status=TaskStatus.FAILURE, title="失败任务", textContent="")
    task.createTime = "2026-10-31 09:00:00"
    history_store.save(task)
    archive.backfill()
    assert not list(archive.archive_dir().glob("*失败任务*.txt"))


def test_archive_filename_sanitized():
    history_store.clear()
    task = TaskDetail(taskId="arch_name_1", taskType="transcript",
                      status=TaskStatus.SUCCESS, title='非法/字符:*?"<>|测试',
                      platform="抖音", textContent="正文")
    task.createTime = "2026-10-31 09:00:00"
    history_store.save(task)
    archive.backfill()
    files = list(archive.archive_dir().glob("*测试*.txt"))
    assert files, "应生成归档"
    assert not any(ch in files[0].name for ch in '\\/:*?"<>|')
