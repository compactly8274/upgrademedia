import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.db import db
from app import tasks

log = logging.getLogger(__name__)
_scheduler = BackgroundScheduler()


def _job_analyze():
    tasks.run_analyze()


def _job_upgrade():
    tasks.run_upgrade()


def _job_scan():
    tasks.run_scan()


def load_schedules():
    _scheduler.remove_all_jobs()
    with db() as conn:
        rows = conn.execute("SELECT * FROM schedules WHERE enabled=1").fetchall()
    for row in rows:
        func = {'analyze': _job_analyze, 'upgrade': _job_upgrade, 'scan': _job_scan}.get(row["job_type"], _job_analyze)
        try:
            _scheduler.add_job(func, CronTrigger.from_crontab(row["cron"]),
                               id=str(row["id"]), replace_existing=True)
            log.info("Scheduled %s job (id=%s) with cron: %s", row["job_type"], row["id"], row["cron"])
        except Exception as exc:
            log.error("Invalid cron for schedule %s: %s", row["id"], exc)


def start():
    _scheduler.start()
    load_schedules()


def stop():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
