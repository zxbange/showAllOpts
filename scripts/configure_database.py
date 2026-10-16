"""Initialize local configuration, or validate an explicitly selected external file.

Never overwrite existing configuration or copy an external secret into the project.
Legacy collector settings are parsed as literals, without executing the source.
"""
import argparse
import ast
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.core.config import ConfigurationError, Settings


def initialize_configuration(root: Path, source: Path | None = None):
    if 'APP_ENV_FILE' in os.environ:
        Settings()
        print('External configuration selected by APP_ENV_FILE; validated and preserved. No local .env created.')
        return
    target = root / 'backend' / '.env'
    if target.exists():
        print('backend/.env already exists; preserved without changes.')
        return
    source_path = source if source is not None else root.parent / 'original' / 'mysqltrade.py'
    if not source_path.is_file():
        if source is not None:
            raise ConfigurationError('The explicitly selected collector source file does not exist.')
        content = (root / 'backend' / '.env.example').read_text(encoding='utf-8')
        message = 'Created backend/.env from the template. Fill in its database settings before starting.'
    else:
        tree = ast.parse(source_path.read_text(encoding='utf-8-sig'))
        config_class = next((node for node in tree.body if isinstance(node, ast.ClassDef)
                            and node.name in ('MysqlTrade', 'MysqlMomentum')), None)
        if config_class is None:
            raise ConfigurationError('Connection configuration class was not found.')
        mapping = {'host': 'DB_HOST', 'port': 'DB_PORT', 'dbname': 'DB_NAME',
                   'dbuser': 'DB_USER', 'dbpassword': 'DB_PASSWORD'}
        values = {}
        for node in config_class.body:
            if isinstance(node, ast.Assign):
                for variable in node.targets:
                    if isinstance(variable, ast.Name) and variable.id in mapping:
                        values[mapping[variable.id]] = ast.literal_eval(node.value)
        if len(values) != len(mapping):
            raise ConfigurationError('Some database settings are missing; configure backend/.env manually.')
        content = '# Local configuration. Never commit this file.\n'
        content += ''.join(f'{key}={json.dumps(str(value), ensure_ascii=False)}\n' for key, value in values.items())
        content += 'CORS_ORIGINS=["http://127.0.0.1:5173","http://localhost:5173"]\n'
        message = 'Created backend/.env from the existing configuration. Credentials were not printed.'
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(content)
    print(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='Optional legacy collector file; used only for local initialization.')
    args = parser.parse_args()
    initialize_configuration(ROOT, args.source)


if __name__ == '__main__':
    main()
