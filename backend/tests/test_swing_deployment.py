from pathlib import Path
from scripts.deploy import application_files, release_files


def test_release_includes_local_skill_and_new_ui_without_secrets(tmp_path):
    expected={'backend/app/main.py','backend/app/analysis_skills/vcp-swing.md',
        'backend/app/static/index.html','backend/app/static/app.js','backend/app/static/styles.css'}
    for name in expected|{'backend/app/.env','backend/app/static/.env','backend/app/__pycache__/x.pyc'}:
        path=tmp_path/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('fixture')
    assert {str(path.relative_to(tmp_path)) for path in application_files(tmp_path)}==expected


def test_offline_release_contains_deployment_module_used_by_tests():
    repo = Path(__file__).resolve().parents[2]
    files = release_files(repo)
    assert repo / 'scripts/deploy.py' in files
    assert repo / 'backend/tests/test_swing_deployment.py' in files
    assert all(path.is_file() for path in files)
    assert all('.env' not in path.parts and '__pycache__' not in path.parts for path in files)
