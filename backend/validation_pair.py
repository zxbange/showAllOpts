"""Run only in the server terminal, as the same account/configuration as the API."""
import argparse
from app.core.config import Settings
from app.services.validation_access import ValidationAccess, AccessError


def main():
    parser = argparse.ArgumentParser(description='领取短线胜率验证网页的一次性配对码（10分钟、最多10次尝试）')
    parser.add_argument('--origin', required=True, help='网页实际来源，例如 http://example.com:8080，不含路径')
    parser.add_argument('--revoke-sessions', action='store_true', help='撤销所有旧浏览器会话后领取新码')
    args = parser.parse_args()
    try:
        code = ValidationAccess(Settings()).issue(args.origin, args.revoke_sessions)
    except (AccessError, ValueError):
        parser.exit(1, '无法领取配对码：请确认与API相同的服务账户和APP_ENV_FILE、持久目录权限；已有管理密钥模式不能被配对绕过。\n')
    print('配对码（10分钟有效、一次性；仅在本站验证页面输入，不要发到聊天或日志）：')
    print(code)


if __name__ == '__main__':
    main()
