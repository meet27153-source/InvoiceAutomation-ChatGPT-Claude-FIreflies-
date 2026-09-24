"""APScheduler wrapper for the configured unattended schedule."""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from app import db
from app.logging_setup import log
from app.orchestrator import run_all

_scheduler = BackgroundScheduler()
_JOB_ID = "invoice_automation_job"


def _build_trigger(schedule_type: str, schedule_value: str):
    if schedule_type == "days":
        return IntervalTrigger(days=int(schedule_value))
    if schedule_type == "monthly":
        return CronTrigger(day=int(schedule_value), hour=6, minute=0)
    if schedule_type == "cron":
        return CronTrigger.from_crontab(schedule_value)
    raise ValueError(f"Unknown schedule_type '{schedule_type}'")


def reschedule():
    settings = db.get_settings()
    trigger = _build_trigger(settings["schedule_type"], settings["schedule_value"])
    if _scheduler.get_job(_JOB_ID):
        _scheduler.remove_job(_JOB_ID)
    _scheduler.add_job(run_all, trigger=trigger, id=_JOB_ID, replace_existing=True, max_instances=1, coalesce=True)
    log.info("Scheduled automation job: type=%s value=%s", settings["schedule_type"], settings["schedule_value"])


def start():
    reschedule()
    if not _scheduler.running:
        _scheduler.start()
        log.info("Scheduler started.")


def shutdown():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
