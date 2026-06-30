"""The notifier is bundle-selected, not a core global: DIGEST_NOTIFIER picks it, and
a front-end that doesn't set it (chat-native, like a future Slack bundle) gets no
separate push. ntfy is the OWUI bundle's choice.

Run:  PYTHONPATH=. python tests/test_notifier.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from digestcore.config import Config
from digestcore.service.server import build_notifier
from digestcore.delivery import NtfyNotifier, NullNotifier


def main():
    assert isinstance(build_notifier(Config(NOTIFIER="ntfy", MEM0_BASE_URL="")), NtfyNotifier), \
        "NOTIFIER=ntfy should give the ntfy notifier"
    assert isinstance(build_notifier(Config(NOTIFIER="none", MEM0_BASE_URL="")), NullNotifier), \
        "NOTIFIER=none should give no notifier"
    assert isinstance(build_notifier(Config(MEM0_BASE_URL="")), NullNotifier), \
        "default (unset) should be no notifier — front-ends opt in"
    print("PASS — notifier is selected by DIGEST_NOTIFIER; ntfy is opt-in (OWUI's choice), "
          "default is none.")


if __name__ == "__main__":
    main()
