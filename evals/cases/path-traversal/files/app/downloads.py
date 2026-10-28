from pathlib import Path

from flask import abort, send_file

UPLOAD_DIR = Path("/srv/uploads")


def download(filename: str):
    path = UPLOAD_DIR / filename
    if not path.is_file():
        abort(404)
    return send_file(path)
