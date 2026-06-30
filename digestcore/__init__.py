"""digestcore — a front-end-agnostic personalized digest engine.

Every interface (OWUI, CLI, Slack, Discord, SMS…) is a thin wrapper over these
capabilities. Nothing here imports a front end.
"""

from digestcore.config import Config
from digestcore.db import open_db
from digestcore.sources import seed_runtime_files
from digestcore.profile import ProfileService
from digestcore.feedback import FeedbackService
from digestcore.subscriptions import SubscriptionService
from digestcore.runner import DigestRunner

__all__ = [
    "Config", "open_db", "seed_runtime_files",
    "ProfileService", "FeedbackService", "SubscriptionService", "DigestRunner",
]
