"""Resource-limited readers retain their last published verified index prefix."""

from __future__ import annotations

import json

from .workspace_index_storage import _json, _text


class IndexReaderMemory:
    def _limit_reader(self, source, reason):
        reader = self.readers.pop(source, None)
        if reader is not None:
            reader.close()
        self._filter_contexts.pop(source, None)
        reason = _text(reason, 2000)
        self._memory_limited[source] = reason
        self._errors[source] = reason
        row = self.db.execute(
            "SELECT payload FROM sources WHERE id=?", (source,)
        ).fetchone()
        payload = json.loads(row[0])
        payload.update(status="RESOURCE_LIMIT", caught_up=False)
        self.db.execute(
            "UPDATE sources SET status='RESOURCE_LIMIT',payload=? WHERE id=?",
            (_json(payload), source),
        )
        self.db.commit()

    def _close_readers(self):
        error = None
        for reader in self.readers.values():
            try:
                reader.close()
            except Exception as exc:
                error = error or exc
        self.readers.clear()
        if error is not None:
            raise error
