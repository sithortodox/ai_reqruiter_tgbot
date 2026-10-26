"""Смоук-проверка веб-панели: удаление ПД и ретеншн (запуск вручную)."""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import models
from app.database.base import Base


def main(tmp: str) -> None:
    url = f"sqlite+aiosqlite:///{tmp}/e2e.db"
    sync_url = f"sqlite:///{tmp}/e2e.db"
    settings = Settings(
        _env_file=None,
        BOT_TOKEN="1:x",
        ADMIN_IDS="1",
        RECRUITERS_CHAT_ID="-1001",
        LLM_API_KEY="",
        DATABASE_URL=url,
        STORAGE_DIR=f"{tmp}/storage",
    )

    eng = create_engine(sync_url)
    Base.metadata.create_all(eng)
    Session = sessionmaker(eng)

    from app.database.base import get_engine, reset_database_state

    reset_database_state()
    get_engine(url)

    from app.admin.web import create_app

    app = create_app(settings)
    tok = base64.b64encode(b"admin:admin").decode()
    h = {"Authorization": "Basic " + tok}
    c = TestClient(app)

    with Session() as db:
        cand = models.Candidate(
            telegram_id=555, telegram_username="u",
            full_name="Тест Тестов", contact_data="+7000",
        )
        db.add(cand)
        vac = models.Vacancy(slug="x", title="V", description="", evaluation_config={})
        db.add(vac)
        db.flush()
        ses = models.InterviewSession(
            candidate_id=cand.id, vacancy_id=vac.id, status="completed"
        )
        db.add(ses)
        db.commit()
        cid, sid = cand.id, ses.id

    r = c.get(f"/candidates/{cid}", headers=h)
    print("candidate page:", r.status_code, "| delete btn:", "/delete" in r.text)

    r = c.post(f"/candidates/{cid}/delete", headers=h, follow_redirects=False)
    print("delete ->", r.status_code, r.headers.get("location"))
    with Session() as db:
        print("candidate gone:", db.get(models.Candidate, cid) is None)
        print("session anon:", db.get(models.InterviewSession, sid).candidate_id is None)
        evs = db.query(models.AuditLog).filter_by(
            event_type="personal_data_deleted"
        ).all()
        print("audit events:", len(evs))

    r = c.post("/candidates/nope/delete", headers=h, follow_redirects=False)
    print("unknown ->", r.status_code)

    r = c.post("/retention/purge", headers=h, follow_redirects=False)
    print("purge ->", r.status_code, r.headers.get("location"))


if __name__ == "__main__":
    import tempfile

    main(tempfile.mkdtemp())