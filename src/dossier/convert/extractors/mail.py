"""Email messages: ``.eml`` (standard library) and Outlook ``.msg`` (olefile, BSD).

Headers (from, to, cc, date, subject) become properties, the body becomes the
section text (plain text preferred, HTML converted otherwise), and attachments
are listed by name. Attachments are not extracted.
"""

from __future__ import annotations

import email
import email.policy
import struct
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from dossier.convert.extractors.base import ExtractContext, ExtractionError
from dossier.convert.extractors.web import html_to_blocks
from dossier.convert.model import (
    Block,
    DocumentIR,
    ExtractionResult,
    ExtractionWarning,
    ListItem,
    Paragraph,
    Section,
    SectionKind,
)


def _body_blocks(text: str | None, html: str | None) -> list[Block]:
    if text and text.strip():
        return [
            Paragraph(text=chunk)
            for chunk in text.replace("\r\n", "\n").split("\n\n")
            if chunk.strip()
        ]
    if html:
        return html_to_blocks(html)[1]
    return []


def _result(
    source_type: str,
    headers: dict[str, str],
    blocks: list[Block],
    attachments: list[str],
) -> ExtractionResult:
    subject = headers.pop("Subject", "") or None
    section_blocks = list(blocks)
    warnings: list[ExtractionWarning] = []
    if attachments:
        section_blocks.append(Paragraph(text="Attachments:"))
        section_blocks.extend(ListItem(text=name) for name in attachments)
        warnings.append(
            ExtractionWarning(
                code="ATTACHMENTS_NOT_EXTRACTED",
                message=f"{len(attachments)} attachment(s) listed, not converted",
            )
        )
    return ExtractionResult(
        document=DocumentIR(
            source_type=source_type,
            title=subject,
            sections=[Section(kind=SectionKind.MESSAGE, blocks=section_blocks)],
            properties={k: v for k, v in headers.items() if v},
        ),
        warnings=warnings,
    )


class EmlExtractor:
    name = "eml"
    version = 1
    suffixes = (".eml",)

    def extract(self, path: Path, ctx: ExtractContext) -> ExtractionResult:
        del ctx
        try:
            message = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
        except Exception as exc:
            raise ExtractionError(
                f"cannot parse message ({type(exc).__name__})", code="corrupt_file", path=path
            ) from exc
        assert isinstance(message, EmailMessage)
        headers = {
            key: str(message.get(key, "") or "") for key in ("From", "To", "Cc", "Date", "Subject")
        }
        text = html = None
        plain_part = message.get_body(preferencelist=("plain",))
        if plain_part is not None:
            text = str(plain_part.get_content())
        html_part = message.get_body(preferencelist=("html",))
        if html_part is not None:
            html = str(html_part.get_content())
        attachments = [part.get_filename() or "unnamed" for part in message.iter_attachments()]
        return _result("eml", headers, _body_blocks(text, html), attachments)


_PT_UNICODE = "001F"
_PT_STRING8 = "001E"
_PT_BINARY = "0102"


def _filetime(value: int) -> datetime | None:
    if value <= 0:
        return None
    return datetime(1601, 1, 1, tzinfo=UTC) + timedelta(microseconds=value // 10)


class MsgExtractor:
    """Minimal Outlook .msg reader over the compound-file streams."""

    name = "msg"
    version = 1
    suffixes = (".msg",)

    def extract(self, path: Path, ctx: ExtractContext) -> ExtractionResult:
        del ctx
        import olefile

        if not olefile.isOleFile(str(path)):
            raise ExtractionError("not an Outlook message file", code="corrupt_file", path=path)
        ole = olefile.OleFileIO(str(path))
        try:
            headers = {
                "From": self._string(ole, "", "0C1A") or self._string(ole, "", "0065"),
                "To": self._string(ole, "", "0E04"),
                "Cc": self._string(ole, "", "0E03"),
                "Date": self._date(ole),
                "Subject": self._string(ole, "", "0037"),
            }
            text = self._string(ole, "", "1000")
            html_bytes = self._binary(ole, "", "1013")
            html = html_bytes.decode("utf-8", errors="replace") if html_bytes else None
            attachments = []
            for entry in ole.listdir(streams=False, storages=True):
                if len(entry) == 1 and entry[0].startswith("__attach_version1.0_"):
                    prefix = entry[0] + "/"
                    name = self._string(ole, prefix, "3707") or self._string(ole, prefix, "3704")
                    attachments.append(name or "unnamed")
        finally:
            ole.close()
        return _result("msg", headers, _body_blocks(text, html), attachments)

    @staticmethod
    def _string(ole: Any, prefix: str, prop: str) -> str:
        for kind, encoding in ((_PT_UNICODE, "utf-16-le"), (_PT_STRING8, "cp1252")):
            stream = f"{prefix}__substg1.0_{prop}{kind}"
            if ole.exists(stream):
                data = ole.openstream(stream).read()
                return str(data.decode(encoding, errors="replace").rstrip("\x00").strip())
        return ""

    @staticmethod
    def _binary(ole: Any, prefix: str, prop: str) -> bytes | None:
        stream = f"{prefix}__substg1.0_{prop}{_PT_BINARY}"
        if ole.exists(stream):
            return bytes(ole.openstream(stream).read())
        return None

    @staticmethod
    def _date(ole: Any) -> str:
        """Client submit time (0x0039) from the fixed-size property stream."""
        stream = "__properties_version1.0"
        if not ole.exists(stream):
            return ""
        data = ole.openstream(stream).read()
        for offset in range(32, len(data) - 15, 16):
            tag, _flags, value = struct.unpack_from("<IIQ", data, offset)
            if tag in (0x00390040, 0x0E060040):
                moment = _filetime(value)
                if moment:
                    return moment.isoformat()
        return ""
