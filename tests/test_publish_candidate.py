"""Publication must be gated by source identity and exact-image validation."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def publisher(tmp_path):
    repo = tmp_path / "checkout"
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests").mkdir()
    shutil.copyfile(Path("scripts/publish-candidate.sh"), repo / "scripts/publish-candidate.sh")
    (repo / "tests/container_smoke.sh").write_text(
        '#!/bin/sh\nprintf "smoke\\n" >> "$CALLS"\nexit "${SMOKE_EXIT:-0}"\n'
    )
    (repo / "tests/container_smoke.sh").chmod(0o755)
    for args in (("init", "-q"), ("config", "user.email", "test@example.invalid"),
                 ("config", "user.name", "Test"), ("add", "."), ("commit", "-qm", "fixture")):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "npm").write_text('#!/bin/sh\nprintf "frontend-build\\n" >> "$CALLS"\n')
    (bin_dir / "docker").write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$CALLS"
if [ "$1 $2" = 'image inspect' ]; then
  case "$*" in
    *Architecture*) echo linux/amd64 ;;
    *Config.Env*) printf 'BUILD_SHA=%s\\n' "$(git rev-parse HEAD)" ;;
    *RepoDigests*) echo '["registry.example.com/cratekeep@sha256:test"]' ;;
  esac
fi
''')
    for path in bin_dir.iterdir():
        path.chmod(0o755)
    calls = tmp_path / "calls"
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
           "CANDIDATE_REGISTRY": "registry.example.com", "CALLS": str(calls)}
    evidence = tmp_path / "evidence"

    def run(**extra):
        result = subprocess.run(["sh", "scripts/publish-candidate.sh", str(evidence)],
                                cwd=repo, env={**env, **extra}, capture_output=True, text=True)
        commands = calls.read_text().splitlines() if calls.exists() else []
        return result, commands

    return repo, evidence, run


def test_failed_smoke_prevents_all_pushes(publisher):
    _, _, run = publisher
    result, commands = run(SMOKE_EXIT="1")
    assert result.returncode != 0
    assert "smoke" in commands
    assert not any(command.startswith("push ") for command in commands)


def test_dirty_source_prevents_build_and_publication(publisher):
    repo, _, run = publisher
    (repo / "uncommitted-source.py").write_text("pass\n")
    result, commands = run()
    assert result.returncode != 0
    assert "local changes" in result.stderr
    assert commands == []


def test_success_publishes_source_tag_then_candidate_after_smoke(publisher):
    repo, evidence, run = publisher
    result, commands = run()
    assert result.returncode == 0, result.stderr
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    pushes = [command for command in commands if command.startswith("push ")]
    assert pushes == [f"push registry.example.com/cratekeep:sha-{sha}",
                      "push registry.example.com/cratekeep:candidate"]
    assert commands.index("smoke") < commands.index(pushes[0])
    assert (evidence / "registry-digests.json").exists()
