import subprocess

from jobservice import db, report, scoring
from jobservice.models import Vacancy


def add(conn, ext, title, company=None):
    db.upsert(conn, Vacancy(source="jobscz", external_id=ext, title=title, url=f"u/{ext}",
                            company=company))  # fmt: skip


def save(conn, vid, qa, barrier="low"):
    s = scoring.parse_score(dict(score_ai=0, score_qa=qa, score_hr=0, entry_barrier=barrier,
                                 fits=[], gaps=[]))  # fmt: skip
    scoring.save_score(conn, vid, s, "m", "h")


def test_summarize_counts_new_since_start_and_best():
    conn = db.connect(":memory:")
    add(conn, "1", "Old QA", "A")
    conn.execute("UPDATE vacancies SET first_seen_at = '2026-01-01T00:00:00+00:00'")
    add(conn, "2", "New QA", "B")
    add(conn, "3", "New Senior", "C")
    conn.execute("UPDATE vacancies SET filter_reason = 'Уровень: senior' WHERE id = 3")
    add(conn, "4", "Decided", "D")
    db.set_status(conn, 4, "apply")
    save(conn, 1, 70)
    save(conn, 2, 55)
    save(conn, 4, 99)  # decided: not "best waiting"

    s = report.summarize(conn, since="2026-06-01T00:00:00+00:00")
    assert (s.found, s.passed, s.to_review) == (3, 1, 2)
    assert s.best["title"] == "Old QA"
    assert s.message() == (
        "Новых вакансий: 3, подходят: 1. Ждут проверки: 2. Лучшая: QA 70, Old QA (A)."
    )


def test_summarize_empty_database():
    s = report.summarize(db.connect(":memory:"), since="2026-01-01")
    assert (s.found, s.passed, s.to_review, s.best) == (0, 0, 0, None)
    assert s.message() == "Новых вакансий: 0, подходят: 0. Ждут проверки: 0."


def test_notify_command_escapes_quotes():
    cmd = report.notify_command("Отчёт", 'Лучшая: "QA" \\ tester')
    assert cmd[:2] == ["osascript", "-e"]
    assert cmd[2] == 'display notification "Лучшая: \\"QA\\" \\\\ tester" with title "Отчёт"'


def test_notify_runs_osascript_on_macos(monkeypatch):
    calls = []
    monkeypatch.setattr(report.sys, "platform", "darwin")
    monkeypatch.setattr(report.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    assert report.notify("T", "M") is True
    assert calls and calls[0][0] == "osascript"


def test_notify_failure_is_not_fatal(monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(report.sys, "platform", "darwin")
    monkeypatch.setattr(report.subprocess, "run", boom)
    assert report.notify("T", "M") is False


def test_notify_skipped_elsewhere(monkeypatch):
    monkeypatch.setattr(report.sys, "platform", "linux")
    monkeypatch.setattr(report.subprocess, "run", lambda *a, **k: 1 / 0)
    assert report.notify("T", "M") is False


def test_cli_report_runs_collect_score_and_notifies(tmp_path, monkeypatch):
    from jobservice import cli

    path = tmp_path / "j.db"
    order, notes = [], []

    def fake_collect(args):
        order.append("collect")
        conn = db.connect(args.db)
        add(conn, "1", "Fresh QA", "Acme")
        conn.close()
        return 0

    def fake_score(args):
        order.append("score")
        conn = db.connect(args.db)
        save(conn, 1, 64)
        conn.close()
        return 1  # e.g. one vacancy failed

    monkeypatch.setattr(cli, "collect", fake_collect)
    monkeypatch.setattr(cli, "score", fake_score)
    monkeypatch.setattr(report, "notify", lambda title, msg: notes.append((title, msg)))
    assert cli.main(["report", "--db", str(path)]) == 1
    assert order == ["collect", "score"]
    title, msg = notes[0]
    assert title == "Отчёт по вакансиям готов"
    assert "Новых вакансий: 1, подходят: 1" in msg and "QA 64, Fresh QA (Acme)" in msg
    assert msg.endswith("Были ошибки, см. лог.")


def test_cli_report_no_notify(tmp_path, monkeypatch):
    from jobservice import cli

    monkeypatch.setattr(cli, "collect", lambda args: 0)
    monkeypatch.setattr(cli, "score", lambda args: 0)
    monkeypatch.setattr(report, "notify", lambda *a: 1 / 0)
    assert cli.main(["report", "--no-notify", "--db", str(tmp_path / "j.db")]) == 0


# ---- e-mail -------------------------------------------------------------------------------------


def test_email_config_from_env():
    assert report.EmailConfig.from_env({}) is None
    assert report.EmailConfig.from_env({"JOBS_GMAIL_ADDRESS": "me@x.cz"}) is None
    cfg = report.EmailConfig.from_env(
        {"JOBS_GMAIL_ADDRESS": " me@x.cz ", "JOBS_GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"}
    )
    assert (cfg.address, cfg.app_password) == ("me@x.cz", "abcdefghijklmnop")


def test_email_goes_only_to_sender():
    msg = report.build_email(report.EmailConfig("me@x.cz", "pw"), "Тема", "Текст")
    assert msg["From"] == msg["To"] == "me@x.cz"
    assert msg["Cc"] is None and msg["Bcc"] is None
    assert msg["Subject"] == "Тема" and "Текст" in msg.get_content()


def test_email_body_lists_waiting_vacancies():
    conn = db.connect(":memory:")
    add(conn, "1", "Good QA", "Acme")
    add(conn, "2", "Unscored")
    save(conn, 1, 64, barrier="medium")
    s = report.summarize(conn, since="2000-01-01")
    body = report.email_body(s, report.waiting(conn), errors=True)
    assert "1. Good QA (Acme)\n   QA 64, порог средний\n   u/1" in body
    assert "2. Unscored\n   без оценки" in body
    assert "были ошибки" in body and report.REVIEW_URL in body
    assert report.email_subject(s) == "Отчёт по вакансиям: подходят 2, ждут проверки 2"
    assert "...и ещё 1." in report.email_body(s, report.waiting(conn, limit=1))


def test_send_email_uses_gmail_ssl(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["server"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, msg, from_addr, to_addrs):
            sent["to"] = to_addrs

    monkeypatch.setattr(report.smtplib, "SMTP_SSL", FakeSMTP)
    report.send_email(report.EmailConfig("me@x.cz", "pw"), "S", "B")
    assert sent == {"server": ("smtp.gmail.com", 465), "login": ("me@x.cz", "pw"),
                    "to": ["me@x.cz"]}  # fmt: skip


def _cli_with_email(tmp_path, monkeypatch, send):
    from jobservice import cli

    monkeypatch.setenv("JOBS_GMAIL_ADDRESS", "me@x.cz")
    monkeypatch.setenv("JOBS_GMAIL_APP_PASSWORD", "pw")
    monkeypatch.setattr(cli, "collect", lambda args: 0)
    monkeypatch.setattr(cli, "score", lambda args: 0)
    notes = []
    monkeypatch.setattr(report, "send_email", send)
    monkeypatch.setattr(report, "notify", lambda title, msg: notes.append(msg))
    code = cli.main(["report", "--db", str(tmp_path / "j.db")])
    return code, notes


def test_cli_report_emails_instead_of_notifying(tmp_path, monkeypatch):
    mails = []
    code, notes = _cli_with_email(tmp_path, monkeypatch, lambda cfg, s, b: mails.append(s))
    assert code == 0 and notes == []
    assert mails == ["Отчёт по вакансиям: подходят 0, ждут проверки 0"]


def test_cli_report_falls_back_to_notification_when_email_fails(tmp_path, monkeypatch):
    import smtplib

    def fail(cfg, s, b):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    code, notes = _cli_with_email(tmp_path, monkeypatch, fail)
    assert code == 0 and notes[0].endswith("Письмо не отправилось, см. лог.")
