"""The resume text every AI path reads must be the file on storage NOW, not a CDN copy.

Every upload overwrites <user_id>/resume.pdf. A plain download kept returning the
previous file for seconds after a re-upload (measured live 10-06, 3 of 3 uploads); a
download with a unique query string returned the new one every time.
"""

from unittest.mock import MagicMock, patch

from modules import ai_cover_letter


def test_each_resume_download_bypasses_the_cdn_copy():
    sb = MagicMock()
    download = sb.storage.from_.return_value.download
    download.side_effect = RuntimeError("stop after the call")  # text extraction not under test
    with patch("app.db.client.get_supabase", return_value=sb):
        ai_cover_letter.load_resume_text("u1/resume.pdf")
        ai_cover_letter.load_resume_text("u1/resume.pdf")
    first, second = (c.kwargs["query_params"]["v"] for c in download.call_args_list)
    assert download.call_args_list[0].args == ("u1/resume.pdf",)
    assert first and second and first != second
