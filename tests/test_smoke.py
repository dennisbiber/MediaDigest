"""Offline end-to-end smoke test.

Proves the core runs with NO front end and NO network: a fake adapter feeds the
real DigestEngine (its two network calls stubbed) through the real DigestRunner
into a capturing StdoutSink, and the real Subscription/Feedback/Profile services
drive it. If this passes, every front end is just a different wrapper over the
same path.

Run:  PYTHONPATH=. python tests/test_smoke.py
"""

import io
import os
import sys
import tempfile

# Make the package importable when run from the repo root without installing.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from digestcore import Config, open_db, SubscriptionService, FeedbackService, DigestRunner
from digestcore.profile import ProfileService
from digestcore.engine import DigestEngine
from digestcore.delivery import StdoutSink
from digestcore.models import Candidate, SourceAdapter
from digestcore import adapters as adapters_pkg


class FakeAdapter(SourceAdapter):
    """Returns a fixed candidate set; no network."""
    signal_weights = {}

    def fetch_candidates(self, topic, window_days, context=None):
        return [
            Candidate(id="a1", title="Local transit plan approved",
                      url="https://example.com/a1", summary="A city transit expansion."),
            Candidate(id="a2", title="New embedding model released",
                      url="https://example.com/a2", summary="An open embedding model."),
            Candidate(id="a3", title="Celebrity gossip roundup",
                      url="https://example.com/a3", summary="Who said what this week."),
        ]


class OfflineEngine(DigestEngine):
    """Force the offline paths: no embeddings (keyword overlap), empty judge
    (score-order backfill). Exercises real scoring/dedup/ordering without Ollama."""
    def _embed(self, text):
        return None

    def _judge(self, profile, shortlist, n, audience=""):
        return []


def main():
    tmp = tempfile.mkdtemp(prefix="digest-smoke-")
    os.environ["DIGEST_DATA_DIR"] = tmp
    cfg = Config(DB_PATH=os.path.join(tmp, "digest.db"), DATA_DIR=tmp,
                 MEM0_BASE_URL="", EMBED_MODEL="", PREF_WEIGHT=1.0)
    db = open_db(cfg.DB_PATH)

    # Register the fake adapter so the runner can find it by name.
    adapters_pkg.ADAPTERS["fake"] = FakeAdapter()

    subs = SubscriptionService(db)
    assert subs.register_user("local", tz="America/Chicago").ok, "register failed"
    add = subs.add_subscription("local", "Smoke", adapter="fake",
                                topic_query="transit", count=5,
                                window_days=7, hour=7, day_of_week="*", day_of_month="*")
    assert add.ok, f"add failed: {add.message}"

    captured = io.StringIO()
    runner = DigestRunner(cfg, db, sink=StdoutSink(stream=captured),
                          engine=OfflineEngine(cfg, db),
                          profile_service=ProfileService(db, cfg))

    rep1 = runner.run_all()
    out1 = captured.getvalue()
    assert rep1.runs and rep1.runs[0].count == 3, f"expected 3 delivered, got {rep1.runs}"
    assert "Smoke" in out1 and "example.com" in out1, "digest text not rendered"
    assert "fake__a" in out1, "item reference marker not surfaced for CLI feedback"

    # Dedup: a second run should deliver nothing new (already-sent).
    rep2 = runner.run_all()
    assert rep2.runs[0].count == 0, f"expected 0 on re-run, got {rep2.runs[0].count}"

    # Feedback -> profile reflects it (no front end involved).
    fb = FeedbackService(db).record("local", "fake", "a1", "up",
                                    title="Local transit plan approved")
    assert fb.ok, f"feedback failed: {fb.message}"
    profile = ProfileService(db, cfg).load("local", "transit")
    assert "transit" in profile.lower(), f"profile missing feedback signal: {profile!r}"

    print("PASS — offline pipeline ran end-to-end with no front end and no network.")
    print(f"  delivered run 1: {rep1.runs[0].count}, re-run (dedup): {rep2.runs[0].count}")
    print(f"  profile after 👍: {profile!r}")
    print("\n--- captured digest (StdoutSink) ---")
    print(out1.strip())


if __name__ == "__main__":
    main()
