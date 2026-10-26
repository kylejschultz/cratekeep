import importlib.util
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location("release_identity", Path(__file__).parents[1] / "scripts/release-identity.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_dev_and_pr_do_not_claim_a_release_version():
    for ref in ("refs/heads/dev", "refs/pull/13/merge"):
        with patch.object(release.subprocess, "run") as git:
            version, tags = release.identity(ref, "abc123")
        assert version == "development"
        assert tags == ["ghcr.io/kylejschultz/cratekeep:abc123", "ghcr.io/kylejschultz/cratekeep:dev"]
        git.assert_not_called()


def test_release_requires_release_ancestry_and_does_not_overwrite_development_sha_tag():
    with patch.object(release.subprocess, "run") as git:
        version, tags = release.identity("refs/tags/v1.0.0", "abc123")
    git.assert_called_once_with(["git", "merge-base", "--is-ancestor", "abc123", "origin/release"], check=True)
    assert version == "1.0.0"
    assert "ghcr.io/kylejschultz/cratekeep:1.0.0" in tags
    assert "ghcr.io/kylejschultz/cratekeep:abc123-1.0.0" in tags
    assert "ghcr.io/kylejschultz/cratekeep:abc123" not in tags


@pytest.mark.parametrize("tag", ["v01.0.0", "v1.0", "v1.0.0-rc.1", "v1.0.0\nextra"])
def test_invalid_release_tags_fail_before_publication(tag):
    with patch.object(release.subprocess, "run") as git, pytest.raises(ValueError):
        release.identity("refs/tags/" + tag, "abc123")
    git.assert_not_called()


def test_unmerged_release_source_is_rejected():
    with patch.object(release.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "git")), pytest.raises(subprocess.CalledProcessError):
        release.identity("refs/tags/v1.0.0", "unmerged")
