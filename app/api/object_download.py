"""Own download streams across completion, send failure and client cancellation."""

from urllib.parse import quote

from fastapi.responses import StreamingResponse


class ObjectDownloadResponse(StreamingResponse):
    def __init__(self, stream, filename):
        self.stream = stream
        super().__init__(
            self.chunks(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)},
        )

    def chunks(self):
        while data := self.stream.read(1024 * 1024):
            yield data

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.stream.close()
