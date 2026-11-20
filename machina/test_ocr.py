import shutil
import subprocess
import pytest
import pymupdf as fitz
from machina import ocr


def scanned_pdf():
    doc = fitz.open()
    page = doc.new_page(width=400, height=120)
    page.insert_text((25, 65), 'ACME ENGINEERING', fontsize=24)
    image = page.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes('png')
    scan = fitz.open()
    page = scan.new_page(width=400, height=120)
    page.insert_image(page.rect, stream=image)
    return scan.tobytes()


@pytest.mark.skipif(not shutil.which('tesseract'), reason='requires local tesseract')
def test_recovers_scanned_identity_without_modifying_pdf():
    data = scanned_pdf()
    report = ocr.extract_pdf(data)
    assert not report['limitations']
    text = ' '.join(line['text'] for page in report['pages'] for line in page['lines'])
    assert 'ACME ENGINEERING' in text
    for line in report['pages'][0]['lines']:
        for word in line['words']:
            assert 0 <= word['bbox'][0] < word['bbox'][2] <= 400
            assert 0 <= word['bbox'][1] < word['bbox'][3] <= 120
    with fitz.open(stream=data, filetype='pdf') as doc:
        assert not doc[0].get_text().strip()


def test_page_limit():
    doc = fitz.open(stream=scanned_pdf(), filetype='pdf')
    doc.new_page()
    report = ocr.extract_pdf(doc.tobytes(), max_pages=1)
    if shutil.which('tesseract'):
        assert len(report['pages']) == 1
        assert 'page_limit_reached' in report['limitations']


def test_timeout_is_reported(monkeypatch):
    monkeypatch.setattr(ocr.shutil, 'which', lambda n: '/test/tesseract')
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('tesseract', 1)
    monkeypatch.setattr(ocr, '_recognize', timeout)
    report = ocr.extract_pdf(scanned_pdf())
    assert report['limitations'] == ['page_ocr_timeout']


def test_invalid_resource_limits():
    with pytest.raises(ValueError):
        ocr.extract_pdf(b'', dpi=1200)
