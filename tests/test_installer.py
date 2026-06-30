"""Test the installer's orchestration logic with docker + prompts stubbed.

Builds a temp deploy/ tree with a fake core, a fake front-end (with a setup hook),
and a fake LLM bundle (declaring core_env), then runs the installer with recording
stubs and asserts the *order* and *effects*: network created, core up, front-end up,
its setup hook ran and approved a token, the LLM's core_env landed in .env, and the
core was refreshed to apply it.

Run:  PYTHONPATH=. python tests/test_installer.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scripts.install as inst


def _bundle(path, toml_text, compose=True, setup_text=None):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "bundle.toml"), "w") as f:
        f.write(toml_text)
    if compose:
        with open(os.path.join(path, "docker-compose.yml"), "w") as f:
            f.write("services: {}\n")
    if setup_text:
        with open(os.path.join(path, "setup.py"), "w") as f:
            f.write(setup_text)


def main():
    tmp = tempfile.mkdtemp(prefix="digest-inst-")
    deploy = os.path.join(tmp, "deploy")
    os.makedirs(os.path.join(deploy, "core"), exist_ok=True)
    open(os.path.join(deploy, "core", "docker-compose.yml"), "w").close()

    _bundle(os.path.join(deploy, "frontends", "owui"),
            'name = "Open WebUI"\ntype = "compose"\n',
            setup_text=("def setup(ctx, bundle):\n"
                        "    ctx.run(['echo', 'owui-setup'])\n"
                        "    ctx.wait_and_approve('owui')\n"))
    _bundle(os.path.join(deploy, "llm", "ollama"),
            'name = "Ollama"\ntype = "compose"\n\n[core_env]\nDIGEST_OLLAMA_BASE_URL = "http://ollama:11434"\n')

    # point the installer's module paths at the temp deploy tree
    inst.DEPLOY = deploy
    inst.CORE_DIR = os.path.join(deploy, "core")
    inst.ENV_FILE = os.path.join(deploy, ".env")

    calls = []
    def runner(cmd, check=True, env=None):
        calls.append(list(cmd))
        class R: returncode = 0
        return R()

    answers = iter(["1", "1"])   # pick option 1 in each phase (only one anyway)
    def asker(prompt, default=""):
        try:
            return next(answers)
        except StopIteration:
            return default

    ctx = inst.Ctx(run=runner, ask=asker, health=lambda url: True,
                   env_file=inst.ENV_FILE, core_dir=inst.CORE_DIR)
    inst.main(ctx)

    joined = [" ".join(c) for c in calls]

    def idx(substr):
        for i, c in enumerate(joined):
            if substr in c:
                return i
        return -1

    assert idx("network create digest-net") >= 0, "network not created"
    core_ups = [i for i, c in enumerate(joined) if "core/docker-compose.yml" in c and " up" in c]
    fe_up = idx("frontends/owui/docker-compose.yml")
    llm_up = idx("llm/ollama/docker-compose.yml")
    assert core_ups, "core never started"
    assert fe_up >= 0 and llm_up >= 0, "front-end or llm not started"
    # order: core (first) -> front-end -> llm
    assert core_ups[0] < fe_up < llm_up, f"phase order wrong: {joined}"
    # setup hook ran and approved the token
    assert idx("echo owui-setup") >= 0, "front-end setup hook did not run"
    assert idx("auth approve owui") >= 0, "token was not approved"
    # llm core_env written to .env
    env_txt = open(inst.ENV_FILE).read()
    assert "DIGEST_OLLAMA_BASE_URL=http://ollama:11434" in env_txt, f".env missing engine: {env_txt!r}"
    # core refreshed AFTER llm to apply the new env
    assert core_ups[-1] > llm_up, "core was not refreshed after the engine choice"

    print("PASS — installer flow: network, core, front-end(+setup+approve), llm(+core_env), "
          "and core refresh ran in the right order; new bundles are pure drop-ins.")


if __name__ == "__main__":
    main()
