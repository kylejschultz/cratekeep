"""Exercise the release-source workflow without checking out or executing PR code."""
import json
from pathlib import Path
import subprocess

import pytest
import yaml

WORKFLOW = Path(__file__).parents[1] / '.github/workflows/release-source.yml'


@pytest.mark.parametrize(('branch', 'repository', 'author', 'labels', 'allowed'), [
    ('dev', 'kylejschultz/cratekeep', 'kylejschultz', [], True),
    ('dev', 'someone/cratekeep', 'someone', [], False),
    ('feature', 'kylejschultz/cratekeep', 'kylejschultz', [], False),
    ('release-please--branches--release', 'kylejschultz/cratekeep', 'kylejschultz', ['autorelease: pending'], True),
    ('release-please--branches--release', 'someone/cratekeep', 'kylejschultz', ['autorelease: pending'], False),
    ('release-please--branches--release', 'kylejschultz/cratekeep', 'someone', ['autorelease: pending'], False),
    ('release-please--branches--release', 'kylejschultz/cratekeep', 'kylejschultz', [], False),
    ('release-please--branches--release--components--cratekeep', 'kylejschultz/cratekeep', 'kylejschultz', ['autorelease: pending'], True),
    ('release-please--branches--release--components--cratekeep', 'someone/cratekeep', 'kylejschultz', ['autorelease: pending'], False),
    ('release-please--branches--release--components--cratekeep', 'kylejschultz/cratekeep', 'someone', ['autorelease: pending'], False),
    ('release-please--branches--release--components--cratekeep', 'kylejschultz/cratekeep', 'kylejschultz', [], False),
    ('release-please--branches--release--components--other', 'kylejschultz/cratekeep', 'kylejschultz', ['autorelease: pending'], False),
])
def test_release_source(branch, repository, author, labels, allowed):
    script = yaml.safe_load(WORKFLOW.read_text())['jobs']['source']['steps'][0]['with']['script']
    context = {
        'repo': {'owner': 'kylejschultz', 'repo': 'cratekeep'},
        'payload': {'pull_request': {
            'head': {'ref': branch, 'repo': {'full_name': repository}},
            'user': {'login': author}, 'labels': [{'name': name} for name in labels],
        }},
    }
    harness = 'const context = ' + json.dumps(context) + ';\n'
    harness += 'let failed = false; const core = {setFailed: () => {failed = true}};\n'
    harness += script + '\nconsole.log(JSON.stringify(!failed));'
    result = subprocess.run(['node', '--input-type=module', '-e', harness], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) is allowed
