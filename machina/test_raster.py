import pymupdf as fitz
from machina.raster import transform_png, transform_pdf, _regions


def source():
    doc = fitz.open()
    page = doc.new_page(width=300, height=160)
    page.insert_text((20, 40), 'ACME ENGINEERING', fontsize=16)
    page.insert_text((20, 100), '25.4 mm', fontsize=14)
    page.draw_line((15, 120), (270, 120), color=(0, 0, 0))
    png = page.get_pixmap(alpha=False).tobytes('png')
    scan = fitz.open()
    page = scan.new_page(width=300, height=160)
    page.insert_image(page.rect, stream=png)
    return png, scan.tobytes()


def ocr():
    return {'limitations': [], 'pages': [{'page': 0, 'lines': [{'words': [
        {'text': 'ACME', 'confidence': 99, 'bbox': [19, 22, 67, 44]},
        {'text': 'ENGINEERING', 'confidence': 99, 'bbox': [69, 22, 190, 44]},
    ]}]}]}


def test_png_only_matched_region_pixels_change():
    png, _ = source()
    output, report = transform_png(png, {'ACME ENGINEERING': 'Company_001'}, ocr_report=ocr())
    before, after = fitz.Pixmap(png), fitz.Pixmap(output)
    assert (before.width, before.height) == (after.width, after.height)
    rect = fitz.IRect(report['regions'][0]['bbox'])
    old, new = before.samples, after.samples
    changes = 0
    for y in range(before.height):
        for x in range(before.width):
            i = (y * before.width + x) * 3
            if old[i:i+3] != new[i:i+3]:
                changes += 1
                assert rect.contains(fitz.Point(x, y))
    assert changes > 0
    assert not report['complete']
    assert report['replacements'] == 1


def test_pdf_only_matched_region_render_changes_and_alias_visible():
    _, pdf = source()
    output, report = transform_pdf(pdf, {'ACME ENGINEERING': 'Company_001'}, ocr_report=ocr())
    with fitz.open(stream=pdf, filetype='pdf') as a, fitz.open(stream=output, filetype='pdf') as b:
        assert 'Company_001' in b[0].get_text()
        old, new = a[0].get_pixmap().samples, b[0].get_pixmap().samples
        for y in range(160):
            for x in range(300):
                if not 18 <= x <= 191 or not 21 <= y <= 45:
                    i = (y * 300 + x) * 3
                    assert old[i:i+3] == new[i:i+3]
    assert report['replacements'] == 1


def test_low_confidence_leaves_pixels_unchanged():
    png, _ = source()
    scan = ocr()
    scan['pages'][0]['lines'][0]['words'][0]['confidence'] = 20
    output, report = transform_png(png, {'ACME ENGINEERING': 'Company_001'}, ocr_report=scan)
    assert fitz.Pixmap(output).samples == fitz.Pixmap(png).samples
    assert report['replacements'] == 0
    assert 'low_confidence_identity_preserved' in report['limitations']


def test_multiline_identity_uses_separate_rectangles():
    lines = [
        {'words': [{'text': 'ACME', 'confidence': 99, 'bbox': [10, 10, 100, 30]}]},
        {'words': [{'text': 'ENGINEERING', 'confidence': 99, 'bbox': [10, 35, 110, 55]}]},
    ]
    edits, limits = _regions(lines, {'ACME ENGINEERING': 'Company_001'}, 85)
    assert not limits
    assert len(edits) == 1
    assert len(edits[0]['boxes']) == 2


def test_distant_labels_not_joined():
    lines = [
        {'words': [{'text': 'ACME', 'confidence': 99, 'bbox': [10, 10, 100, 30]}]},
        {'words': [{'text': 'ENGINEERING', 'confidence': 99, 'bbox': [10, 100, 110, 120]}]},
    ]
    edits, limits = _regions(lines, {'ACME ENGINEERING': 'Company_001'}, 85)
    assert not edits
    assert 'disconnected_identity_tokens_preserved' in limits
