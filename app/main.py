"""Entry point: initialize storage, start scheduler, then serve local UI."""
from app import db, scheduler
from app.config import WEB_HOST, WEB_PORT
from app.logging_setup import log
from app.web.routes import create_app


def main():
    db.init_db()
    log.info("Database initialized.")
    scheduler.start()
    flask_app = create_app()
    log.info("Starting web UI at http://%s:%s", WEB_HOST, WEB_PORT)
    try:
        flask_app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    main()
