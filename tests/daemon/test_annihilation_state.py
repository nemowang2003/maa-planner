from maa_planner.daemon.coordinator import Daemon
from maa_planner.daemon.execution.annihilation import AnnihilationResult
from maa_planner.daemon.execution.context import RunResult
from maa_planner.daemon.planning.annihilation import AnnihilationDecision
from maa_planner.daemon.planning.schedule import ScheduledOccurrence
from maa_planner.daemon.policy import ConfigSnapshot
from maa_planner.daemon.state import StateStore
from tests.helpers.planning import MONDAY


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("maa_planner.daemon.scheduling.time.time", lambda: MONDAY + 100)
    daemon = Daemon(tmp_path / "config.toml", tmp_path / "control")
    daemon.store = StateStore(tmp_path / "state.sqlite3")
    return daemon


def begin(daemon, identifier):
    occurrence = ScheduledOccurrence(identifier, "nemo", MONDAY, MONDAY + 86400)
    daemon.store.claim_schedule(occurrence, "revision")
    daemon.daily.attempts[identifier] = occurrence, ConfigSnapshot("revision", b"")
    daemon.daily.annihilation_plans[identifier] = AnnihilationDecision(
        True, MONDAY, MONDAY + 7 * 86400, "run"
    )
    daemon.daily.running(identifier)


def test_missing_proxy_alert_does_not_persist_attempt_or_completion(tmp_path, monkeypatch):
    daemon = setup(tmp_path, monkeypatch)
    begin(daemon, "first")
    daemon.daily.finished(
        RunResult(
            "first",
            "failed",
            annihilation_results=(
                AnnihilationResult(
                    1,
                    "Annihilation",
                    "copilot_required",
                    "no_auto_deploy",
                    observed_at=MONDAY + 100,
                ),
            ),
        )
    )
    assert daemon.store.alerts("nemo")[0]["category"] == "annihilation_copilot"
    begin(daemon, "second")
    daemon.daily.finished(
        RunResult(
            "second",
            "succeeded",
            annihilation_results=(
                AnnihilationResult(
                    1, "Annihilation", "completed", "weekly_quota", 1800, 1800, MONDAY + 200
                ),
            ),
        )
    )
    assert not daemon.store.alerts("nemo")
    daemon.store.close()


def test_callback_across_reset_does_not_complete_old_cycle(tmp_path, monkeypatch):
    daemon = setup(tmp_path, monkeypatch)
    begin(daemon, "crossing")
    daemon.daily.finished(
        RunResult(
            "crossing",
            "succeeded",
            annihilation_results=(
                AnnihilationResult(
                    1, "Annihilation", "completed", "weekly_quota", 1800, 1800, MONDAY + 7 * 86400
                ),
            ),
        )
    )
    assert not daemon.store.db.execute(
        "SELECT name FROM sqlite_master WHERE name='weekly_annihilation'"
    ).fetchall()
    daemon.store.close()


def test_legacy_weekly_flags_are_removed_without_affecting_other_state(tmp_path):
    import sqlite3

    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 10")
        db.execute("CREATE TABLE weekly_annihilation (profile TEXT, payload TEXT)")
        db.execute("INSERT INTO weekly_annihilation VALUES ('nemo', '{}')")
    store = StateStore(path)
    assert not store.db.execute(
        "SELECT name FROM sqlite_master WHERE name='weekly_annihilation'"
    ).fetchall()
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 14
    store.close()
