"""Raw-artefact storage.

Raw source files are written to the local `${DATA_DIR}/raw/<category>/<key>.<ext>` tree and
referenced by a `file://` URI. An S3 mirror is prepared but disabled (see `save_raw`); because
the `raw_storage_uri` column is opaque, switching to `s3://<bucket>/raw/...` URIs needs no
migration.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from regsuper.config import get_settings


@dataclass(frozen=True)
class StoredArtefact:
    uri: str  # local file:// or s3://
    local_path: Path  # always present; the pipeline re-reads from here
    sha256: str
    size: int


def save_raw(category: str, key: str, ext: str, data: bytes) -> StoredArtefact:
    """Persist raw bytes and return the artefact handle.

    `category` is a logical bucket (e.g. 'eurlex', 'esbirka', 'cnb').
    `key` is a unique-within-category identifier (e.g. CELEX ID).
    `ext` is the file extension without the dot.
    """
    settings = get_settings()
    target_dir = settings.data_dir / "raw" / category
    target_dir.mkdir(parents=True, exist_ok=True)
    local_path = target_dir / f"{key}.{ext}"
    local_path.write_bytes(data)

    sha = hashlib.sha256(data).hexdigest()
    uri = local_path.resolve().as_uri()

    # The S3 mirror is disabled so local development needs no credentials. Enabling it means
    # uncommenting the hook below.
    # if settings.s3_access_key:
    #     _upload_to_s3(category, key, ext, data)
    #     uri = f"s3://{settings.s3_bucket}/raw/{category}/{key}.{ext}"

    return StoredArtefact(uri=uri, local_path=local_path, sha256=sha, size=len(data))
