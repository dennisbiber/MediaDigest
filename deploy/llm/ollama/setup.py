"""Post-up hook for the Ollama bundle: wait for the server, then pull the models the
digest core needs — the agent/judge model and the embedding model."""
import time

MODELS = ["qwen3:14b", "nomic-embed-text"]   # judge + embed (see core defaults)


def setup(ctx, bundle):
    print("  Waiting for the Ollama container to be ready…")
    for _ in range(30):
        try:
            ctx.run(["docker", "exec", "ollama", "ollama", "list"])
            break
        except Exception:  # noqa: BLE001
            time.sleep(2)
    for m in MODELS:
        print(f"  Pulling {m} (this can take a while)…")
        ctx.run(["docker", "exec", "ollama", "ollama", "pull", m], check=False)
