import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pymupdf
from fastapi import HTTPException, Response
from PIL import Image
from starlette.requests import Request

from app.api.deps import authorize_chat_session
from app.api.auth import _issue_browser_access_token
from app.config import FRONTEND_ORIGIN, get_allowed_origins
from app.services.pdf_parser import PDFResourceLimitError, extract_text_from_pdf
from app.services.security import create_guest_access_token, decode_access_token
from app.services.retriever import MAX_SECTION_CONTEXT_CHUNKS, retrieve_chunks
from app.services.rate_limit import enforce_rate_limit


class GuestAccessTests(unittest.TestCase):
    def setUp(self):
        self.session_id = "00d0d37c-05ab-45a0-9a13-7213d78f3288"
        self.session = SimpleNamespace(id=self.session_id, user_id=None, is_guest=True)
        self.origin = next(origin.strip() for origin in FRONTEND_ORIGIN.split(",") if origin.strip())

    def make_request(self, origin):
        return Request({
            "type": "http",
            "scheme": "https",
            "server": ("example.test", 443),
            "method": "GET",
            "path": "/chats",
            "headers": [(b"origin", origin.encode("ascii"))],
        })

    def test_valid_guest_credential_is_bound_and_refreshed(self):
        response = Response()
        authorize_chat_session(
            self.session,
            None,
            create_guest_access_token(self.session_id),
            self.make_request(self.origin),
            response,
        )
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("guest_access=", cookie)
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertIn("secure", cookie)

    def test_credential_for_another_session_is_rejected(self):
        with self.assertRaises(HTTPException) as error:
            authorize_chat_session(
                self.session,
                None,
                create_guest_access_token("another-session"),
                self.make_request(self.origin),
                Response(),
            )
        self.assertEqual(error.exception.status_code, 403)

    def test_unapproved_origin_is_rejected(self):
        with self.assertRaises(HTTPException) as error:
            authorize_chat_session(
                self.session,
                None,
                create_guest_access_token(self.session_id),
                self.make_request("https://evil.example"),
                Response(),
            )
        self.assertEqual(error.exception.status_code, 403)

    def test_localhost_and_ipv4_loopback_are_allowed_aliases(self):
        configured_hosts = {
            origin.rsplit("/", 1)[-1]
            for origin in get_allowed_origins("http://localhost:8000")
        }
        self.assertTrue(
            any(host.startswith("localhost") for host in configured_hosts)
            and any(host.startswith("127.0.0.1") for host in configured_hosts)
        )

    def test_account_login_cookie_is_http_only_and_secure_on_https(self):
        response = Response()
        token_response = _issue_browser_access_token(
            response,
            self.make_request(self.origin),
            "user-id",
        )
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("secure", cookie)
        self.assertEqual(decode_access_token(token_response.access_token), "user-id")


class SectionContextTests(unittest.TestCase):
    def test_long_section_is_bounded_and_marked_as_truncated(self):
        chunks = [
            {"text": f"section part {index}", "page": index, "source": "doc.pdf"}
            for index in range(MAX_SECTION_CONTEXT_CHUNKS + 1)
        ]
        with patch("app.services.retriever.count_documents", return_value=1), \
             patch("app.services.retriever.get_distinct_section_titles", return_value=["Skills"]), \
             patch("app.services.retriever.get_chunks_by_section", return_value=chunks) as lookup:
            selected, mode = retrieve_chunks("skills", "skills", "session-id", db=None)

        self.assertEqual(mode, "specific")
        self.assertEqual(len(selected), MAX_SECTION_CONTEXT_CHUNKS)
        self.assertTrue(selected[0]["context_truncated"])
        self.assertTrue(all(chunk["match_type"] == "section_exact" for chunk in selected))
        self.assertEqual(lookup.call_args.kwargs["limit"], MAX_SECTION_CONTEXT_CHUNKS + 1)


class OCRFallbackTests(unittest.TestCase):
    def make_image_pdf(self, path: Path):
        image_path = path.with_suffix(".png")
        Image.new("RGB", (32, 32), "white").save(image_path)
        document = pymupdf.open()
        page = document.new_page()
        page.insert_image(page.rect, filename=str(image_path))
        document.save(path)
        document.close()

    def test_low_text_image_page_appends_ocr_and_preserves_text(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            pdf_path = Path(directory) / "mixed.pdf"
            self.make_image_pdf(pdf_path)
            with patch("app.services.pdf_parser._get_body_font_size", return_value=10.0), \
                 patch("app.services.pdf_parser._extract_structured_text", return_value="Pg 1"), \
                 patch("app.services.pdf_parser._ocr_page", return_value="Recognized scanned body") as ocr:
                pages = extract_text_from_pdf(str(pdf_path))

        ocr.assert_called_once()
        self.assertIn("Pg 1", pages[0]["text"])
        self.assertIn("Recognized scanned body", pages[0]["text"])

    def test_sufficient_selectable_text_skips_ocr(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            pdf_path = Path(directory) / "digital.pdf"
            self.make_image_pdf(pdf_path)
            selectable_text = "A sufficiently long selectable text block for this page."
            with patch("app.services.pdf_parser._get_body_font_size", return_value=10.0), \
                 patch("app.services.pdf_parser._extract_structured_text", return_value=selectable_text), \
                 patch("app.services.pdf_parser._ocr_page") as ocr:
                pages = extract_text_from_pdf(str(pdf_path))

        ocr.assert_not_called()
        self.assertEqual(pages[0]["text"], selectable_text)

    def test_ocr_is_skipped_when_page_exceeds_raster_budget(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            pdf_path = Path(directory) / "large_page.pdf"
            image_path = Path(directory) / "small.png"
            Image.new("RGB", (32, 32), "white").save(image_path)
            document = pymupdf.open()
            page = document.new_page(width=20_000, height=20_000)
            page.insert_image(page.rect, filename=str(image_path))
            document.save(pdf_path)
            document.close()
            with patch("app.services.pdf_parser._get_body_font_size", return_value=10.0), \
                 patch("app.services.pdf_parser._extract_structured_text", return_value=""), \
                 patch("app.services.pdf_parser._ocr_page") as ocr:
                pages = extract_text_from_pdf(str(pdf_path))

        ocr.assert_not_called()
        self.assertEqual(pages, [])

    def test_pdf_page_count_limit_is_enforced(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            pdf_path = Path(directory) / "too_many_pages.pdf"
            document = pymupdf.open()
            for _ in range(2):
                document.new_page()
            document.save(pdf_path)
            document.close()
            with patch("app.services.pdf_parser.MAX_PDF_PAGES", 1):
                with self.assertRaises(PDFResourceLimitError):
                    extract_text_from_pdf(str(pdf_path))


class RateLimitTests(unittest.TestCase):
    def test_exceeded_limit_returns_429_and_persists_counter(self):
        request = Request({
            "type": "http",
            "scheme": "https",
            "server": ("example.test", 443),
            "client": ("192.0.2.1", 12345),
            "method": "POST",
            "path": "/auth/guest",
            "headers": [],
        })
        db = MagicMock()
        db.execute.return_value.one.return_value = (3, datetime.utcnow())

        with self.assertRaises(HTTPException) as error:
            enforce_rate_limit(
                request,
                db,
                scope="guest_session",
                limit=2,
                window_seconds=900,
            )

        self.assertEqual(error.exception.status_code, 429)
        self.assertIn("Retry-After", error.exception.headers)
        db.commit.assert_called_once()




if __name__ == "__main__":
    unittest.main()
