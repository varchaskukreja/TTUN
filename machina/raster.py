"""Opt-in OCR-guided known-identity replacement, never a blanket raster scrub.

All changed rectangles require review: geometry underneath identifying text
cannot be recovered. OCR false negatives and logos remain possible. PNG results
preserve decoded RGB pixels outside approved rectangles, not original metadata.
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


def transform_png(data: bytes, replacements: dict[str, str], *, ocr_report=None,
                  min_confidence=85, max_pixels=16_000_000, padding=1, **ocr_options) -> tuple[bytes, dict]:
    """Return lossless RGB PNG with exact unchanged pixels outside edited boxes.

    Optional ocr_report coordinates must use original image pixels, unlike
    transform_pdf's PDF points. Input is restricted to nontransparent RGB PNG.
    """
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('Only PNG input is supported by this lossless handler')
    # Read IHDR dimensions before decoding to enforce the allocation bound.
    width, height = int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')
    if width < 1 or height < 1 or width * height > max_pixels:
        raise ValueError('PNG exceeds pixel budget or has invalid dimensions')
    pix = fitz.Pixmap(data)
    if pix.alpha or pix.n != 3:
        raise ValueError('Only RGB PNG without alpha is supported')
    if not 0 <= min_confidence <= 100 or not 0 <= padding <= 5:
        raise ValueError('Invalid OCR confidence threshold')
    if ocr_report is None:
        # One PDF point per source pixel avoids coordinate ambiguity.
        with fitz.open() as wrapper:
            page = wrapper.new_page(width=width, height=height)
            page.insert_image(page.rect, stream=data)
            ocr_report = extract_pdf(wrapper.tobytes(), max_pixels=max_pixels, **ocr_options)
    limits = set(ocr_report['limitations']) | {'ocr_false_negatives_and_logos_require_review'}
    edits, extra = _regions(ocr_report['pages'][0]['lines'] if ocr_report['pages'] else [], replacements, min_confidence, padding)
    limits |= extra
    samples = bytearray(pix.samples)
    report = {'format': 'png-raster', 'replacements': 0, 'regions': [], 'complete': False}
    for edit in edits:
        rectangles = [fitz.IRect(math.floor(b.x0), math.floor(b.y0), math.ceil(b.x1), math.ceil(b.y1)) & pix.irect for b in edit['boxes']]
        if any(r.is_empty for r in rectangles):
            limits.add('invalid_ocr_region_preserved')
            continue
        for index, rect in enumerate(rectangles):
            with fitz.open() as patch:
                page = patch.new_page(width=rect.width, height=rect.height)
                if index == 0:
                    page.insert_text((0, edit['fontsize']), edit['alias'], fontsize=edit['fontsize'], color=(0, 0, 0))
                replacement = page.get_pixmap(alpha=False).samples
            for row in range(rect.height):
                start = ((rect.y0 + row) * width + rect.x0) * 3
                source = row * rect.width * 3
                samples[start:start + rect.width * 3] = replacement[source:source + rect.width * 3]
            report['regions'].append({'bbox': list(rect)})
        report['replacements'] += 1
    if report['replacements']:
        limits.add('geometry_inside_replacement_regions_requires_review')
    report['limitations'] = sorted(limits)
    return fitz.Pixmap(fitz.csRGB, width, height, bytes(samples), False).tobytes('png'), report
