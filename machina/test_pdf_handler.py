import pymupdf as fitz
from machina.pdf_handler import extract, transform


def fixture():
    doc = fitz.open()
    p = doc.new_page(width=400, height=300)
    p.insert_text((30, 40), 'Acme Engineering', fontsize=12)
    p.insert_text((30, 80), 'Tolerance 0.01 mm; 25.4 mm', fontsize=10)
    p.insert_text((30, 110), 'AcmeX', fontsize=10)
    p.draw_rect((20, 20, 250, 150), color=(0, 0, 1))
    p.draw_line((20, 35), (250, 35), color=(1, 0, 0))
    doc.set_metadata({'author': 'Acme Engineering', 'title': 'Drawing'})
    return doc


def test_text_and_metadata_preserve_geometry():
    doc = fixture()
    before = doc[0].get_drawings()
    result, report = transform(doc.tobytes(), {'Acme Engineering': 'Company_001', 'Acme': 'Org_001'})
    output = fitz.open(stream=result, filetype='pdf')
    text = output[0].get_text()
    assert 'Company_001' in text
    assert 'Acme Engineering' not in text
    assert 'AcmeX' in text
    assert 'Tolerance 0.01 mm; 25.4 mm' in text
    assert output.metadata['author'] == 'Company_001'
    assert [{k: v for k, v in d.items() if k != 'seqno'} for d in output[0].get_drawings()] == [{k: v for k, v in d.items() if k != 'seqno'} for d in before]
    assert report['replacements'] == 1
    assert b'Acme Engineering' not in b''.join(output.xref_stream(x) or b'' for x in range(1, output.xref_length()))


def test_attachments_explicitly_flagged_and_preserved():
    doc = fixture()
    doc.embfile_add('part.step', b'original native model')
    result, report = transform(doc.tobytes(), {'Acme Engineering': 'Company_001'})
    out = fitz.open(stream=result, filetype='pdf')
    assert out.embfile_get('part.step') == b'original native model'
    assert not report['complete']
    assert 'embedded_files_require_recursive_sanitization' in report['limitations']


def test_annotations_links_and_xmp():
    doc = fixture()
    p = doc[0]
    p.add_text_annot((280, 70), 'Acme Engineering')
    p.insert_link({'kind': fitz.LINK_URI, 'from': fitz.Rect(20, 180, 150, 200), 'uri': 'https://acme.example/contact'})
    doc.set_xml_metadata('<root><author>Acme Engineering</author></root>')
    result, report = transform(doc.tobytes(), {'Acme Engineering': 'Company_001', 'acme.example': 'org001.example'})
    out = fitz.open(stream=result, filetype='pdf')
    page = out[0]
    assert next(page.annots()).info['content'] == 'Company_001'
    assert page.get_links()[0]['uri'] == 'https://org001.example/contact'
    assert 'Company_001' in out.get_xml_metadata()
    assert any('Company_001' in x for x in extract(result))


def test_too_long_pseudonym_is_not_silently_erased():
    doc = fixture()
    result, report = transform(doc.tobytes(), {'Acme Engineering': 'Company_' + 'x' * 500})
    out = fitz.open(stream=result, filetype='pdf')
    assert 'Acme Engineering' in out[0].get_text()
    assert 'pseudonym_too_small_to_read' in report['limitations']


def test_raster_image_preserved_and_flagged():
    doc = fixture()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 10, 10), False)
    pix.clear_with(100)
    doc[0].insert_image(fitz.Rect(30, 30, 90, 45), pixmap=pix)
    source_digest = doc[0].get_image_info(hashes=True)[0]['digest']
    result, report = transform(doc.tobytes(), {'Acme Engineering': 'Company_001'})
    out = fitz.open(stream=result, filetype='pdf')
    assert out[0].get_image_info(hashes=True)[0]['digest'] == source_digest
    assert 'raster_images_require_ocr_and_logo_review' in report['limitations']


def test_blank_pseudonyms_rejected():
    import pytest
    with pytest.raises(ValueError):
        transform(fixture().tobytes(), {'Acme Engineering': ''})
