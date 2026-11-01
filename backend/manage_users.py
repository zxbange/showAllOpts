"""服务器本地创建首个管理员或重置密码；密码仅交互输入，不写入参数和日志。"""
import argparse
import getpass

from app.core.config import Settings
from app.services.accounts import Accounts, AccountError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['init', 'reset-password'])
    parser.add_argument('--username', required=True)
    args = parser.parse_args()
    first = getpass.getpass('请输入密码（至少12个字符）: ')
    second = getpass.getpass('再次输入密码: ')
    if first != second:
        parser.error('两次密码不一致')
    try:
        accounts = Accounts(Settings())
        if args.operation == 'init':
            accounts.create(args.username, first, bootstrap=True)
        else:
            accounts.reset_from_console(args.username, first)
    except AccountError as error:
        parser.error(str(error))
    print('账号已更新。请使用网站统一登录。')


if __name__ == '__main__':
    main()
