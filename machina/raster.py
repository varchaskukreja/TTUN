"""Opt-in OCR-guided known-identity replacement, never a blanket raster scrub.

All changed rectangles require review: geometry underneath identifying text
cannot be recovered. OCR false negatives and logos remain possible. PNG results
preserve decoded pixels outside approved rectangles. Text metadata uses the same
identity mapping; unsupported metadata surfaces are explicitly reported.
"""
from __future__ import annotations

import math
import re
import pymupdf as fitz
try:
    from .ocr import extract_pdf
    from .pdf_handler import _replacer
except ImportError:
    from ocr import extract_pdf
    from pdf_handler import _replacer


def _regions(lines, replacements, confidence, padding=1):
    replace, pattern = _replacer(replacements)
    if pattern is None:
        return [], set()
    tokens = []
    text = ''
    for line_id, line in enumerate(lines):
        for word in line['words']:
            if text:
                text += ' '
            start = len(text)
            text += word['text']
            tokens.append((start, len(text), line_id, word))
    regions, limits = [], set()
    for match in pattern.finditer(text):
        selected = [t for t in tokens if t[0] < match.end() and t[1] > match.start()]
        if any(t[3]['confidence'] < confidence for t in selected):
            limits.add('low_confidence_identity_preserved')
            continue
        boxes = {}
        for _, _, line_id, word in selected:
            rect = fitz.Rect(word['bbox'])
            boxes[line_id] = boxes[line_id] | rect if line_id in boxes else rect
        boxes = list(boxes.values())
        # Adjacent OCR lines may form one name, but never join distant labels.
        if any(b.y0 - a.y1 > max(a.height, b.height) * 1.5 or b.y0 < a.y0 or
               abs(a.x0 - b.x0) > max(a.width, b.width) for a, b in zip(boxes, boxes[1:])):
            limits.add('disconnected_identity_tokens_preserved')
            continue
        alias = replace(text[selected[0][0]:selected[-1][1]])
        size = min(boxes[0].height / 1.4, boxes[0].width / max(fitz.get_text_length(alias, fontsize=1), .001))
        if size < 3 or any(ord(c) > 127 for c in alias) or '\n' in alias:
            limits.add('unrenderable_pseudonym_preserved')
            continue
        boxes = [fitz.Rect(b.x0-padding, b.y0-padding, b.x1+padding, b.y1+padding) for b in boxes]
        regions.append({'boxes': boxes, 'alias': alias, 'fontsize': size})
    return regions, limits


def transform_pdf(data: bytes, replacements: dict[str, str], *, ocr_report=None,
                  min_confidence=85, padding=1, **ocr_options) -> tuple[bytes, dict]:
    """Opt-in image-pixel redaction for matched OCR identities.

    OCR coordinates are in rendered-page points. Rotated pages are skipped;
    vector paths remain in the PDF but white replacement boxes can cover paths
    crossing an identity. Run the metadata/attachment handler separately.
    """
    if not 0 <= min_confidence <= 100 or not 0 <= padding <= 5:
        raise ValueError('Invalid OCR confidence threshold')
    scan = ocr_report if ocr_report is not None else extract_pdf(data, **ocr_options)
    limits = set(scan['limitations']) | {'ocr_false_negatives_and_logos_require_review', 'metadata_and_attachments_require_separate_processing'}
    report = {'format': 'pdf-raster', 'replacements': 0, 'regions': [], 'complete': False}
    with fitz.open(stream=data, filetype='pdf') as doc:
        for result in scan['pages']:
            page = doc[result['page']]
            if page.rotation:
                limits.add('rotated_raster_page_preserved')
                continue
            edits, extra = _regions(result['lines'], replacements, min_confidence, padding)
            limits |= extra
            accepted = []
            images = [fitz.Rect(i['bbox']) for i in page.get_image_info()]
            for edit in edits:
                if not all(any(image.contains(box) for image in images) for box in edit['boxes']):
                    limits.add('identity_outside_raster_preserved')
                    continue
                accepted.append(edit)
            if not accepted:
                continue
            for edit in accepted:
                for box in edit['boxes']:
                    page.add_redact_annot(box, fill=(1, 1, 1), cross_out=False)
                    report['regions'].append({'page': result['page'], 'bbox': list(box)})
            page.apply_redactions(images=2, graphics=0, text=0)
            for edit in accepted:
                box = edit['boxes'][0]
                page.insert_text((box.x0, box.y0 + edit['fontsize']), edit['alias'],
                                 fontsize=edit['fontsize'], color=(0, 0, 0))
            report['replacements'] += len(accepted)
        report['limitations'] = sorted(limits | ({'geometry_inside_replacement_regions_requires_review'} if report['replacements'] else set()))
        return doc.tobytes(garbage=4, deflate=False), report


def transform_png(data: bytes, replacements: dict[str, str], **options) -> tuple[bytes, dict]:
    """Compatibility wrapper for mode-preserving lossless PNG processing."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Expected PNG input")
    return transform_image(data, replacements, **options)


def _metadata_value(value, replace, limits):
    if isinstance(value, str):
        return replace(value)
    if isinstance(value, bytes):
        try:
            return replace(value.decode("utf-8")).encode("utf-8")
        except UnicodeError:
            limits.add("binary_image_metadata_preserved_requires_review")
            return value
    if isinstance(value, tuple):
        return tuple(_metadata_value(v, replace, limits) for v in value)
    if isinstance(value, dict):
        return {k: _metadata_value(v, replace, limits) for k, v in value.items()}
    return value


def transform_image(data: bytes, replacements: dict[str, str], *, ocr_report=None,
                    min_confidence=85, max_pixels=16_000_000, max_frames=20,
                    padding=1, **ocr_options) -> tuple[bytes, dict]:
    """Preserve PNG/JPEG/TIFF format, frames, modes, and engineering metadata.

    Lossless outputs retain decoded pixels outside reported rectangles exactly.
    JPEG is re-encoded at quality 100 with no chroma subsampling, explicitly
    reported as lossy. Supplied OCR coordinates use raw image pixels (before
    EXIF orientation), with page index equal to TIFF frame index. Alpha is
    unchanged outside edits; replacement rectangles are opaque for legibility.
    Unknown binary metadata is preserved and flagged. Unsupported modes, frames,
    or dimensions raise before returning any partial output.
    """
    import io
    import struct
    from PIL import Image, PngImagePlugin, TiffImagePlugin
    if not 0 <= min_confidence <= 100 or not 0 <= padding <= 5 or max_frames < 1 or max_pixels < 1:
        raise ValueError("Invalid image processing limits")
    replace, _ = _replacer(replacements)
    limits = {"ocr_false_negatives_and_logos_require_review"}
    report = {"format": "", "replacements": 0, "regions": [], "complete": False,
              "pixel_preservation": "exact_outside_regions", "frames": 0}
    frames = []
    save_options = []
    total_pixels = 0
    with Image.open(io.BytesIO(data)) as original:
        kind = original.format
        if kind not in {"PNG", "JPEG", "TIFF"}:
            raise ValueError("Supported raster formats are PNG, JPEG, and TIFF")
        count = getattr(original, "n_frames", 1)
        if count > max_frames or kind == "PNG" and count != 1:
            raise ValueError("Frame limit exceeded or animated PNG unsupported")
        report.update(format=kind.lower()+"-raster", frames=count)
        for frame_index in range(count):
            original.seek(frame_index)
            width, height = original.size
            total_pixels += width * height
            if total_pixels > max_pixels or min(width, height) < 1:
                raise ValueError("Image exceeds total decoded pixel budget")
            if original.mode not in {"1", "L", "LA", "RGB", "RGBA", "CMYK", "I;16", "I;16B", "I;16L"}:
                raise ValueError("Unsupported image mode: " + original.mode)
            # TIFF loaders may apply orientation while decoding; avoid writing
            # a second orientation transform until a verified normalization path exists.
            if kind == "TIFF" and original.getexif().get(274, 1) != 1:
                raise ValueError("Oriented TIFF requires explicit normalization")
            frame = original.copy()
            options = {}
            if original.info.get("dpi"):
                options["dpi"] = original.info["dpi"]
            if original.info.get("icc_profile"):
                options["icc_profile"] = original.info["icc_profile"]
                limits.add("icc_profile_preserved_requires_identity_review")
            exif = original.getexif()
            clean_exif = Image.Exif()
            for key, value in exif.items():
                if key in (34665, 34853):
                    value = exif.get_ifd(key)
                    if key == 34853:
                        limits.add("gps_metadata_requires_review")
                clean_exif[key] = _metadata_value(value, replace, limits)
            if kind != "TIFF" and clean_exif:
                options["exif"] = clean_exif.tobytes()
                limits.add("embedded_exif_thumbnail_requires_review")
            if kind == "PNG":
                info = PngImagePlugin.PngInfo()
                for key, value in getattr(original, "text", {}).items():
                    info.add_itxt(replace(key), replace(value))
                if "gamma" in original.info:
                    info.add(b"gAMA", struct.pack(">I", round(original.info["gamma"] * 100000)))
                if "srgb" in original.info:
                    info.add(b"sRGB", bytes([original.info["srgb"]]))
                if "chromaticity" in original.info:
                    info.add(b"cHRM", struct.pack(">8I", *(round(v*100000) for v in original.info["chromaticity"])))
                # Retain simple ancillary chunks that do not contain free text.
                # Report other unknown chunks instead of silently claiming full metadata coverage.
                offset = 8
                known = {b"IHDR",b"IDAT",b"IEND",b"iTXt",b"tEXt",b"zTXt",b"gAMA",b"sRGB",b"cHRM",b"pHYs",b"iCCP",b"eXIf",b"tRNS"}
                while offset + 12 <= len(data):
                    size = int.from_bytes(data[offset:offset+4], "big")
                    chunk = data[offset+4:offset+8]
                    body = data[offset+8:offset+8+size]
                    if offset + size + 12 > len(data):
                        raise ValueError("Truncated PNG chunk")
                    if chunk in {b"bKGD",b"tIME"}:
                        info.add(chunk, body)
                    elif chunk not in known:
                        limits.add("unhandled_png_ancillary_chunks_omitted_requires_review")
                    offset += size + 12
                options["pnginfo"] = info
                if "transparency" in original.info:
                    options["transparency"] = original.info["transparency"]
            elif kind == "JPEG":
                if "comment" in original.info:
                    options["comment"] = _metadata_value(original.info["comment"], replace, limits)
                if "xmp" in original.info:
                    options["xmp"] = _metadata_value(original.info["xmp"], replace, limits)
                if any(marker not in {"APP0","APP1","APP2","APP14","COM"} for marker, _ in getattr(original,"applist",[])):
                    limits.add("unhandled_jpeg_application_metadata_omitted_requires_review")
                options.update(quality=100, subsampling=0)
                limits.add("jpeg_recompression_changes_pixels_outside_regions")
                report["pixel_preservation"] = "lossy_jpeg_recompression"
            else:
                # Rebuild layout-dependent tags, preserve remaining metadata.
                structural = {256,257,258,259,262,273,277,278,279,284,317,320,322,323,324,325,330,338,339,347,34665,34853}
                if 330 in original.tag_v2:
                    limits.add("tiff_subifds_omitted_requires_review")
                tags = TiffImagePlugin.ImageFileDirectory_v2()
                for key, value in original.tag_v2.items():
                    if key in structural:
                        continue
                    tags[key] = _metadata_value(value, replace, limits)
                    tags.tagtype[key] = original.tag_v2.tagtype[key]
                for pointer in (34665, 34853):
                    if pointer in clean_exif:
                        tags[pointer] = clean_exif[pointer]
                options.update(tiffinfo=tags, compression="tiff_adobe_deflate")
            if ocr_report is None:
                # OCR composite only; source alpha and pixels remain untouched.
                preview = frame.convert("RGBA")
                background = Image.new("RGBA", frame.size, "white")
                background.alpha_composite(preview)
                buf = io.BytesIO(); background.convert("RGB").save(buf, format="PNG")
                with fitz.open() as wrapper:
                    page = wrapper.new_page(width=width, height=height)
                    page.insert_image(page.rect, stream=buf.getvalue())
                    scan = extract_pdf(wrapper.tobytes(), max_pixels=max_pixels, **ocr_options)
                lines = scan["pages"][0]["lines"] if scan["pages"] else []
                limits.update(scan["limitations"])
            else:
                limits.update(ocr_report["limitations"])
                found = [p for p in ocr_report["pages"] if p["page"] == frame_index]
                lines = found[0]["lines"] if found else []
                if not found:
                    limits.add("frame_not_ocr_processed")
            edits, extra = _regions(lines, replacements, min_confidence, padding)
            limits |= extra
            for edit in edits:
                rectangles = [fitz.IRect(math.floor(b.x0), math.floor(b.y0), math.ceil(b.x1), math.ceil(b.y1)) & fitz.IRect(0,0,width,height) for b in edit["boxes"]]
                if any(r.is_empty for r in rectangles):
                    limits.add("invalid_ocr_region_preserved"); continue
                for index, rect in enumerate(rectangles):
                    with fitz.open() as patch:
                        page = patch.new_page(width=rect.width, height=rect.height)
                        if index == 0:
                            page.insert_text((0, edit["fontsize"]), edit["alias"], fontsize=edit["fontsize"], color=(0,0,0))
                        pix = page.get_pixmap(alpha=False)
                        replacement = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    if frame.mode.startswith("I;16"):
                        # Expand 8-bit patch to the full 16-bit range without
                        # converting any of the original engineering pixels.
                        import array
                        values = array.array("H", (v*257 for v in replacement.convert("L").tobytes()))
                        import sys
                        if (frame.mode == "I;16B") != (sys.byteorder == "big"):
                            values.byteswap()
                        replacement = Image.frombytes(frame.mode, replacement.size, values.tobytes())
                    else:
                        replacement = replacement.convert(frame.mode)
                    frame.paste(replacement, (rect.x0,rect.y0))
                    report["regions"].append({"page": frame_index, "bbox": list(rect)})
                report["replacements"] += 1
            frames.append(frame); save_options.append(options)
    output = io.BytesIO()
    if kind == "TIFF" and len(frames) > 1:
        with TiffImagePlugin.AppendingTiffWriter(output) as writer:
            for frame, options in zip(frames, save_options):
                frame.save(writer, format="TIFF", **options)
                writer.newFrame()
    else:
        frames[0].save(output, format=kind, **save_options[0])
    if report["replacements"]:
        limits.add("geometry_inside_replacement_regions_requires_review")
    report["limitations"] = sorted(limits)
    return output.getvalue(), report
