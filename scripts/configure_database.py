"""Initialize local configuration, or validate an explicitly selected external file.

Never overwrite existing configuration or copy an external secret into the project.
Collector settings are parsed as literals, without executing the source.
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


def read_database_settings(source_path: Path):
    mapping = {'host': 'DB_HOST', 'port': 'DB_PORT', 'dbname': 'DB_NAME',
               'dbuser': 'DB_USER', 'dbpassword': 'DB_PASSWORD'}
    try:
        tree = ast.parse(source_path.read_text(encoding='utf-8-sig'))
    except SyntaxError:
        raise ConfigurationError('Collector source is not valid Python; check its configuration manually.') from None
    config_assignment = next((node for node in tree.body if isinstance(node, ast.Assign)
                              and any(isinstance(target, ast.Name) and target.id == 'DB_CONFIG'
                                      for target in node.targets)), None)
    try:
        if config_assignment is not None:
            database_config = ast.literal_eval(config_assignment.value)
            if not isinstance(database_config, dict):
                raise ConfigurationError('DB_CONFIG must be a literal dictionary.')
            values = {target: database_config[key] for key, target in mapping.items()
                      if key in database_config}
        else:
            config_class = next((node for node in tree.body if isinstance(node, ast.ClassDef)
                                 and node.name in ('MysqlTrade', 'MysqlMomentum')), None)
            if config_class is None:
                raise ConfigurationError('DB_CONFIG or a legacy connection configuration class was not found.')
            values = {}
            for node in config_class.body:
                if isinstance(node, ast.Assign):
                    for variable in node.targets:
                        if isinstance(variable, ast.Name) and variable.id in mapping:
                            values[mapping[variable.id]] = ast.literal_eval(node.value)
    except ConfigurationError:
        raise
    except (ValueError, TypeError):
        raise ConfigurationError('Database settings must contain literal values only.') from None
    if len(values) != len(mapping):
        raise ConfigurationError('Some database settings are missing; configure the selected file manually.')
    return values


def initialize_configuration(root: Path, source: Path | None = None, production: bool = False):
    if 'APP_ENV_FILE' in os.environ:
        Settings()
        print('External configuration selected by APP_ENV_FILE; validated and preserved. No local .env created.')
        return
    target = root / 'config' / 'app.config' if production else root / 'backend' / '.env'
    label = target.relative_to(root).as_posix()
    if target.exists():
        print(f'{label} already exists; preserved without changes.')
        return
    source_path = source if source is not None else root / '脚本' / '本地专用' / '聚宽采集' / '全部策略批量采集_数据库版.py'
    local_config = root / 'backend' / '.env'
    if production and source is None and local_config.is_file():
        content = local_config.read_text(encoding='utf-8')
        message = 'Created config/app.config from the existing local configuration. Credentials were not printed.'
    elif not source_path.is_file():
        if source is not None:
            raise ConfigurationError('The explicitly selected collector source file does not exist.')
        template = root / 'config' / 'app.config.example' if production else root / 'backend' / '.env.example'
        content = template.read_text(encoding='utf-8')
        message = f'Created {label} from the template. Fill in its database settings before starting.'
    else:
        values = read_database_settings(source_path)
        content = '# Local configuration. Never commit this file.\n'
        content += ''.join(f'{key}={json.dumps(str(value), ensure_ascii=False)}\n' for key, value in values.items())
        content += 'CORS_ORIGINS=["http://127.0.0.1:5173","http://localhost:5173"]\n'
        message = f'Created {label} from the existing configuration. Credentials were not printed.'
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(content)
    print(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='Optional collector file with DB_CONFIG or legacy connection constants.')
    parser.add_argument('--production', action='store_true', help='Initialize config/app.config for Linux startup.')
    args = parser.parse_args()
    initialize_configuration(ROOT, args.source, production=args.production)


if __name__ == '__main__':
    main()
