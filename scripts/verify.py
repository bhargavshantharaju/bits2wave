"""Run the repository's clean-checkout verification sequence."""

import pathlib
import os
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]


def run(label, command, env):
    print(f"\n== {label} ==", flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def check_python_syntax():
    roots = (ROOT / "backend", ROOT / "scripts", ROOT / "tests")
    files = sorted(path for base in roots for path in base.rglob("*.py"))
    for path in files:
        source = path.read_text(encoding="utf-8")
        compile(source, str(path), "exec")
    print(f"Python syntax OK ({len(files)} files)", flush=True)


def main():
    if sys.version_info < (3, 10):
        raise SystemExit("NexAlert requires Python 3.10 or newer (PEP 604 union type syntax is used).")
    check_python_syntax()
    with tempfile.TemporaryDirectory(prefix="nexalert-verify-") as temp_dir:
        runtime_dir = pathlib.Path(temp_dir)
        env = os.environ.copy()
        env.update({
            "NEXALERT_ENV": "test",
            "NEXALERT_SECRET": os.urandom(48).hex(),
            "NEXALERT_DB": str(runtime_dir / "sender.sqlite"),
            "NEXALERT_CC_DB": str(runtime_dir / "receiver.sqlite"),
            "NEXALERT_FALLBACK_DB": str(runtime_dir / "fallback.sqlite"),
            "NEXALERT_LOG_DIR": str(runtime_dir / "logs"),
        })
        run("Unit and integration tests", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], env)
        run("HTTP smoke test", [sys.executable, "scripts/e2e_http_smoke.py"], env)
    print("\nVerification completed successfully.", flush=True)


if __name__ == "__main__":
    main()
