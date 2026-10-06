"""Файлы резервных копий: имя, путь и защита от path traversal."""
import os
import re

BACKUP_FILENAME_RE = re.compile(r'^backup_\d{8}_\d{6}\.json$')


def safe_backup_path(filename: str, backups_dir: str):
    """Полный путь к бэкапу внутри backups_dir либо None.

    None — если имя не соответствует формату backup_YYYYMMDD_HHMMSS.json
    или путь выходит за каталог бэкапов (защита от '../../etc/passwd').
    """
    if not filename or not BACKUP_FILENAME_RE.fullmatch(filename):
        return None
    path = os.path.join(backups_dir, filename)
    if os.path.realpath(path).startswith(os.path.realpath(backups_dir)):
        return path
    return None
