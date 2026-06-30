"""Guard against drift between the canonical OWUI pipeline and its deploy copy.

The pipelines container loads deploy/frontends/owui/pipeline/digest_pipeline.py (a
copy, because OWUI Pipelines must load a directory containing only the pipeline). If
the canonical interfaces/owui/digest_pipeline.py changes and the copy doesn't, the
deployed pipeline goes stale silently. This test fails loudly when they diverge.

Run:  PYTHONPATH=. python tests/test_deploy_sync.py
"""
import os
import filecmp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
canonical = os.path.join(ROOT, "interfaces", "owui", "digest_pipeline.py")
deploy_copy = os.path.join(ROOT, "deploy", "frontends", "owui", "pipeline", "digest_pipeline.py")


def main():
    assert os.path.exists(deploy_copy), f"missing deploy pipeline copy: {deploy_copy}"
    assert filecmp.cmp(canonical, deploy_copy, shallow=False), (
        "deploy/frontends/owui/pipeline/digest_pipeline.py is out of sync with "
        "interfaces/owui/digest_pipeline.py — re-copy it.")
    print("PASS — deploy pipeline copy matches the canonical source.")


if __name__ == "__main__":
    main()
