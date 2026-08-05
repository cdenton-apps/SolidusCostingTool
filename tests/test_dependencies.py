from __future__ import annotations

from streamlit.web.server.starlette.starlette_gzip_middleware import (
    _MediaAwareGZipResponder,
)


def test_streamlit_gzip_responder_matches_pinned_starlette() -> None:
    async def app(scope, receive, send):
        return None

    responder = _MediaAwareGZipResponder(app, minimum_size=500, compresslevel=9)

    assert responder.minimum_size == 500
