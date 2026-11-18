"""Read-only preview by default; archive, restore, or explicitly confirmed one-time purge."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.core.config import Settings
from app.services.collection_retention import retain_history, restore_history, purge_history


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm-archive-history', action='store_true')
    parser.add_argument('--restore', action='store_true')
    parser.add_argument('--permanent', action='store_true', help='Irreversibly remove older terminal history; no automatic mode')
    parser.add_argument('--confirm-permanent-delete-history', action='store_true')
    args = parser.parse_args()
    if args.permanent and args.restore:
        parser.error('--permanent cannot be combined with --restore')
    if args.permanent and args.apply and not args.confirm_permanent_delete_history:
        parser.error('--permanent --apply requires --confirm-permanent-delete-history')
    if args.apply and not args.permanent and not args.confirm_archive_history:
        parser.error('--apply requires --confirm-archive-history')
    settings = Settings()
    action = purge_history if args.permanent else (restore_history if args.restore else retain_history)
    confirmed = args.confirm_permanent_delete_history if args.permanent else args.confirm_archive_history
    result = {'unified': action(settings, apply=args.apply, confirmed=confirmed)}
    from app.services.etf_collection_history import prune_native_history, restore_native_history, purge_native_history
    native_action = purge_native_history if args.permanent else (restore_native_history if args.restore else prune_native_history)
    result['etf_native'] = native_action(settings, apply=args.apply, confirmed=confirmed)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
