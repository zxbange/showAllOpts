"""Real Git regression for the runtime link left by a successful deployment."""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
loader = importlib.machinery.SourceFileLoader('runtime_git_entry', str(ROOT / 'deploy/automation/showallopts-ssh'))
spec = importlib.util.spec_from_loader(loader.name, loader)
entry = importlib.util.module_from_spec(spec)
loader.exec_module(entry)


def test_second_release_accepts_managed_link_but_rejects_other_dirty_files(tmp_path, monkeypatch):
    repo, remote = tmp_path / 'checkout', tmp_path / 'remote.git'
    repo.mkdir()
    env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_AUTHOR_NAME': 'Offline test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
           'GIT_COMMITTER_NAME': 'Offline test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}

    def git(*args):
        return subprocess.run(['git', *args], cwd=repo, env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    git('init', '--bare', str(remote))
    git('init', '-b', 'main')
    git('remote', 'add', 'origin', str(remote))
    (repo / '.gitignore').write_bytes((ROOT / '.gitignore').read_bytes())
    (repo / 'scripts').mkdir()
    (repo / 'scripts/deploy.py').write_text('# first release\n')
    git('add', '.')
    git('commit', '-m', 'first release')
    first = git('rev-parse', 'HEAD')
    (repo / 'scripts/deploy.py').write_text('# next release\n')
    git('commit', '-am', 'next release')
    second = git('rev-parse', 'HEAD')
    git('push', 'origin', 'main')
    git('reset', '--hard', first)
    (repo / 'backend/.venv').mkdir(parents=True)
    (repo / 'backend/.venv/placeholder').write_text('old environment')

    def entry_git(*args):
        # Only origin identity is substituted. All status, fetch, ancestry and
        # source reads run real Git against this isolated offline repository.
        if args == ('remote', 'get-url', 'origin'):
            return 'https://github.com/zxbange/showAllOpts.git'
        return git(*args)

    monkeypatch.setattr(entry, 'git', entry_git)
    assert entry.reviewed_runner(first) == '# first release'
    stage = repo / '.runtime/releases/first'
    stage.mkdir(parents=True)
    (repo / 'backend/.venv').rename(stage / 'previous-venv')
    (stage / 'venv').mkdir()
    (repo / 'backend/.venv').symlink_to(stage / 'venv', target_is_directory=True)
    assert git('status', '--porcelain') == ''
    assert entry.reviewed_runner(second) == '# next release'
    git('merge', '--ff-only', '--no-overwrite-ignore', second)
    assert git('branch', '--show-current') == 'main'
    assert git('status', '--porcelain') == ''

    # Neither unknown backend files nor unrelated environment links are hidden.
    for relative in ('backend/unknown.txt', 'frontend/.venv'):
        path = repo / relative
        path.parent.mkdir(exist_ok=True)
        if relative.endswith('.venv'):
            path.symlink_to(stage / 'venv', target_is_directory=True)
        else:
            path.write_text('must be reviewed')
        with pytest.raises(ValueError, match='clean main'):
            entry.reviewed_runner(second)
        path.unlink()
    (repo / 'scripts/deploy.py').write_text('# unreviewed change\n')
    with pytest.raises(ValueError, match='clean main'):
        entry.reviewed_runner(second)
