"""Import explicitly approved repairs; dry-run unless --apply is provided."""

import argparse
import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.config import Settings
from packages.recruitment_core.jd_repair_import import import_jd_repairs
from packages.storage import Storage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--job-id', required=True, action='append')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    settings = Settings()
    loaded = yaml.safe_load(settings.candidate_profile_config.read_text(encoding='utf-8'))
    profile = loaded.get('profile', loaded)
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    report = json.loads(args.report.read_text(encoding='utf-8'))
    storage = Storage.from_url(settings.database_url)
    try:
        result = import_jd_repairs(storage, report, profile, args.job_id,
                                   apply=args.apply, backup=manifest.get('backup'))
    finally:
        storage.engine.dispose()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
