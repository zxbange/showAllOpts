"""Copy connection settings from the existing collector into a local ignored .env.

Only parses Python literals with ast; never imports or executes trading code.
Existing configuration is preserved. No credentials are printed.
"""
import argparse
import ast
import json
import os
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=root.parent / 'original' / 'mysqltrade.py')
    args = parser.parse_args()
    target = root / 'backend' / '.env'
    if target.exists():
        print('backend/.env already exists; preserved without changes.')
        return
    tree = ast.parse(args.source.read_text(encoding='utf-8-sig'))
    config_class = next((node for node in tree.body if isinstance(node, ast.ClassDef)
                         and node.name in ('MysqlTrade', 'MysqlMomentum')), None)
    if config_class is None:
        raise SystemExit('Connection configuration class was not found.')
    mapping = {'host': 'DB_HOST', 'port': 'DB_PORT', 'dbname': 'DB_NAME',
               'dbuser': 'DB_USER', 'dbpassword': 'DB_PASSWORD'}
    values = {}
    for node in config_class.body:
        if isinstance(node, ast.Assign):
            for variable in node.targets:
                if isinstance(variable, ast.Name) and variable.id in mapping:
                    values[mapping[variable.id]] = ast.literal_eval(node.value)
    if len(values) != len(mapping):
        raise SystemExit('Some database settings are missing; configure backend/.env manually.')
    content = '# Local configuration. Never commit this file.\n'
    content += ''.join(f'{key}={json.dumps(str(value), ensure_ascii=False)}\n' for key, value in values.items())
    content += 'CORS_ORIGINS=["http://127.0.0.1:5173","http://localhost:5173"]\n'
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(content)
    print('Created backend/.env from the existing configuration. Credentials were not printed.')


if __name__ == '__main__':
    main()
