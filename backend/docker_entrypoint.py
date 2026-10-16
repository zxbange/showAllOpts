"""Prepare the mounted log directory, then run the API as the unprivileged user."""
import os
import pwd
import sys

from app.core.config import Settings


def main():
    directory = Settings().log_path
    directory.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        user = pwd.getpwnam('appuser')
        os.chown(directory, user.pw_uid, user.pw_gid)
        os.chmod(directory, 0o750)
        os.setgroups([])
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == '__main__':
    main()
