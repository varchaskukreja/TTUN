"""Conservative PDF identity replacement; reports surfaces requiring manual review.

This is not an OCR/logo or embedded-file sanitizer. The report is a submission
readiness input, not a guarantee that every identifier has been discovered.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import pymupdf as fitz
from .matching import compile_identity_mapping, replace_identity_match


def _replacer(replacements):
    if any(not isinstance(k, str) or not isinstance(v, str) or not k or not v.strip() for k, v in replacements.items()):
        raise ValueError("Identity and pseudonym must be nonempty strings")
    pattern, mapping = compile_identity_mapping(replacements)
    if pattern is None:
        return lambda text: text, None
    return lambda text: pattern.sub(lambda m: replace_identity_match(m, mapping), text), pattern


def extract(data: bytes) -> list[str]:
    """Return candidate text, not a classification of identity versus engineering data."""
    with fitz.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ValueError("Encrypted PDF requires a password")
        result = [v for v in doc.metadata.values() if isinstance(v, str) and v]
        if doc.get_xml_metadata():
            result.append(doc.get_xml_metadata())
        for page in doc:
            result.extend(page.get_text().splitlines())
            for annot in page.annots() or ():
                result.extend(v for v in annot.info.values() if isinstance(v, str) and v)
            for link in page.get_links():
                result.extend(link[k] for k in ("uri", "file") if isinstance(link.get(k), str))
            for widget in page.widgets() or ():
                result.extend(v for v in (widget.field_name, widget.field_label, widget.field_value) if isinstance(v, str) and v)
        return result


def _validate_attachment(name, content, report):
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError("Invalid sanitized attachment name")
    if not isinstance(content, bytes) or not isinstance(report, dict):
        raise ValueError("Invalid sanitized attachment result")
    if report.get("complete") is not True or report.get("limitations"):
        raise ValueError("Attachment sanitizer did not confirm complete processing")


def transform(data: bytes, replacements: dict[str, str], sanitize_attachment=None) -> tuple[bytes, dict]:
    """Replace known identities, preserving vectors/images and engineering text.

    Only horizontal text is rewritten. Embedded/raster content and arbitrary
    annotation appearances are reported for downstream handling. Original
    attachment contents are preserved and MUST NOT be considered sanitized.
    Optional sanitize_attachment(name, bytes) returns (name, bytes, report).
    Only a report with complete=True and no limitations authorizes replacement;
    callback failure retains the original attachment and flags the PDF.
    """
    replace, pattern = _replacer(replacements)
    report = {"format": "pdf", "replacements": 0, "limitations": [], "pages": 0, "attachments_sanitized": 0}
    limits = set()
    has_annotation_attachment = False
    with fitz.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ValueError("Encrypted PDF requires a password")
        report["pages"] = len(doc)
        for name in list(doc.embfile_names()):
            if sanitize_attachment is None:
                limits.add("embedded_files_require_recursive_sanitization")
                continue
            try:
                info = doc.embfile_info(name)
                newname, content, child = sanitize_attachment(info.get("filename") or name, doc.embfile_get(name))
                _validate_attachment(newname, content, child)
                if newname != name and newname in doc.embfile_names():
                    raise ValueError("Attachment name collision")
                if newname == name:
                    doc.embfile_upd(name, buffer_=content, filename=newname, ufilename=newname, desc=replace(info.get("description", "")) if info.get("description") != info.get("filename") else newname)
                else:
                    # Add before delete so failure never silently drops a source file.
                    doc.embfile_add(newname, content, filename=newname, ufilename=newname, desc=replace(info.get("description", "")) if info.get("description") != info.get("filename") else newname)
                    doc.embfile_del(name)
                report["attachments_sanitized"] += 1
            except Exception:
                # Do not expose original attachment names or callback exception text.
                limits.add("embedded_file_sanitization_failed_or_incomplete")
        metadata = dict(doc.metadata)
        allowed = {"title", "author", "subject", "keywords", "creator", "producer", "creationDate", "modDate", "trapped"}
        doc.set_metadata({k: replace(v) for k, v in metadata.items() if k in allowed and isinstance(v, str)})
        xmp = doc.get_xml_metadata()
        if xmp:
            try:
                root = ET.fromstring(xmp)
                for node in root.iter():
                    if node.text:
                        node.text = replace(node.text)
                    if node.tail:
                        node.tail = replace(node.tail)
                    for key, value in node.attrib.items():
                        node.attrib[key] = replace(value)
                doc.set_xml_metadata(ET.tostring(root, encoding="unicode"))
            except ET.ParseError:
                limits.add("malformed_xmp_preserved")
        toc = doc.get_toc(simple=False)
        if toc:
            for entry in toc:
                entry[1] = replace(entry[1])
            doc.set_toc(toc)
        for page in doc:
            if page.get_images():
                limits.add("raster_images_require_ocr_and_logo_review")
            # Update annotations before text redaction invalidates page objects.
            for annot in page.annots() or ():
                if annot.type[0] == fitz.PDF_ANNOT_FILE_ATTACHMENT:
                    has_annotation_attachment = True
                    if sanitize_attachment is None:
                        limits.add("annotation_attachments_require_recursive_sanitization")
                    else:
                        try:
                            info = annot.file_info
                            newname, content, child = sanitize_attachment(info.get("filename", "attachment"), annot.get_file())
                            _validate_attachment(newname, content, child)
                            annot.update_file(buffer_=content, filename=newname, ufilename=newname, desc=replace(info.get("description", "")) if info.get("description") != info.get("filename") else newname)
                            report["attachments_sanitized"] += 1
                        except Exception:
                            limits.add("annotation_attachment_sanitization_failed_or_incomplete")
                changes = {key: replace(annot.info.get(key, "")) for key in ("content", "title", "subject")}
                if any(value != annot.info.get(key, "") for key, value in changes.items()):
                    annot.set_info(**changes)
                    annot.update()
                if annot.type[0] not in (fitz.PDF_ANNOT_TEXT, fitz.PDF_ANNOT_FREE_TEXT):
                    limits.add("annotation_appearances_require_review")
            for link in page.get_links():
                changed = False
                for key in ("uri", "file"):
                    if isinstance(link.get(key), str):
                        new = replace(link[key])
                        changed |= new != link[key]
                        link[key] = new
                if changed:
                    page.update_link(link)
            for widget in page.widgets() or ():
                changed = False
                for name in ("field_name", "field_label", "field_value"):
                    value = getattr(widget, name, None)
                    if isinstance(value, str) and replace(value) != value:
                        setattr(widget, name, replace(value))
                        changed = True
                if changed:
                    widget.update()
                limits.add("form_actions_and_appearance_streams_require_review")
            if pattern is None:
                continue
            edits = []
            for block in page.get_text("rawdict")["blocks"]:
                for line in block.get("lines", []):
                    chars = [(char, span) for span in line["spans"] for char in span["chars"]]
                    text = "".join(char["c"] for char, _ in chars)
                    for match in pattern.finditer(text):
                        if tuple(line["dir"]) != (1.0, 0.0):
                            limits.add("rotated_identity_text_requires_review")
                            continue
                        selected = chars[match.start():match.end()]
                        if not selected:
                            continue
                        rect = fitz.Rect(selected[0][0]["bbox"])
                        for char, _ in selected[1:]:
                            rect |= fitz.Rect(char["bbox"])
                        alias = replace(match.group())
                        if any(ord(c) > 127 for c in alias) or "\n" in alias:
                            limits.add("non_ascii_or_multiline_pseudonym_requires_font_support")
                            continue
                        span = selected[0][1]
                        size = min(span["size"], rect.width / max(fitz.get_text_length(alias, fontsize=1), 0.001))
                        if size < 3:
                            limits.add("pseudonym_too_small_to_read")
                            continue
                        color = span.get("color", 0)
                        rgb = ((color >> 16 & 255) / 255, (color >> 8 & 255) / 255, (color & 255) / 255)
                        edits.append((rect, selected[0][0]["origin"], alias, size, rgb))
            if edits:
                for rect, *_ in edits:
                    page.add_redact_annot(rect, fill=False, cross_out=False)
                # The key safeguard: remove text only, never drawing geometry or images.
                page.apply_redactions(images=0, graphics=0, text=0)
                for rect, origin, alias, size, rgb in edits:
                    page.insert_text(origin, alias, fontsize=size, fontname="helv", color=rgb)
                report["replacements"] += len(edits)
        # Full rewrite drops orphaned original text and previous incremental versions.
        # PyMuPDF 1.26.6 double-compresses updated FileAttachment streams
        # when deflate=True. A full uncompressed rewrite preserves their bytes.
        output = doc.tobytes(garbage=4, deflate=not has_annotation_attachment)
    report["limitations"] = sorted(limits)
    report["complete"] = not limits
    return output, report
