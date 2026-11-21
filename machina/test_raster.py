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


def test_png_grayscale_alpha_and_metadata_preserved():
    import io
    from PIL import Image, PngImagePlugin
    from machina.raster import transform_image
    for mode in ('L', 'LA', 'RGBA'):
        image = Image.new(mode, (300, 160), {'L': 170, 'LA': (170, 100), 'RGBA': (100, 150, 200, 75)}[mode])
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text('Author', 'ACME ENGINEERING')
        metadata.add_text('Units', 'mm')
        source = io.BytesIO(); image.save(source, format='PNG', dpi=(300,300), pnginfo=metadata)
        output, report = transform_image(source.getvalue(), {'ACME ENGINEERING':'Company_001'}, ocr_report=ocr())
        with Image.open(io.BytesIO(output)) as result:
            assert result.mode == mode
            assert result.format == 'PNG'
            assert result.info['Author'] == 'Company_001'
            assert result.info['Units'] == 'mm'
            assert abs(result.info['dpi'][0] - 300) < 0.1
            assert result.crop((0,50,300,160)).tobytes() == image.crop((0,50,300,160)).tobytes()
            if 'A' in mode:
                assert result.getpixel((200,100))[-1] == image.getpixel((200,100))[-1]
                assert result.getpixel((21,23))[-1] == 255
        assert report['pixel_preservation'] == 'exact_outside_regions'


def test_jpeg_format_dimensions_exif_and_lossy_warning():
    import io
    from PIL import Image
    from machina.raster import transform_image
    image = Image.new('RGB', (300,160), (170,180,190))
    exif = Image.Exif(); exif[315] = 'ACME ENGINEERING'; exif[274] = 6
    source = io.BytesIO(); image.save(source, format='JPEG', exif=exif, dpi=(200,200))
    output, report = transform_image(source.getvalue(), {'ACME ENGINEERING':'Company_001'}, ocr_report=ocr())
    with Image.open(io.BytesIO(output)) as result:
        assert result.format == 'JPEG'
        assert result.size == image.size
        assert result.getexif()[315] == 'Company_001'
        assert result.getexif()[274] == 6
        assert result.info['dpi'] == (200,200)
    assert report['pixel_preservation'] == 'lossy_jpeg_recompression'
    assert 'jpeg_recompression_changes_pixels_outside_regions' in report['limitations']


def test_tiff_multiframe_preserves_pixels_and_per_frame_metadata():
    import io
    from PIL import Image, TiffImagePlugin
    from machina.raster import transform_image
    frames = [Image.new('L', (300,160), 125), Image.new('L',(300,160),220)]
    source = io.BytesIO()
    with TiffImagePlugin.AppendingTiffWriter(source) as writer:
        for i,frame in enumerate(frames):
            tags = TiffImagePlugin.ImageFileDirectory_v2()
            tags[315] = 'ACME ENGINEERING'
            tags[270] = 'Frame '+str(i)
            tags[282] = 300+i
            tags[283] = 300+i
            tags[296] = 2
            frame.save(writer, format='TIFF', tiffinfo=tags)
            writer.newFrame()
    scan = ocr(); scan['pages'].append({'page':1, 'lines':[]})
    output, report = transform_image(source.getvalue(), {'ACME ENGINEERING':'Company_001'}, ocr_report=scan)
    with Image.open(io.BytesIO(output)) as result:
        assert result.format == 'TIFF'
        assert result.n_frames == 2
        for i in range(2):
            result.seek(i)
            assert result.tag_v2[315] == 'Company_001'
            assert result.tag_v2[270] == 'Frame '+str(i)
            assert result.tag_v2[282] == 300+i
            assert result.crop((0,50,300,160)).tobytes() == frames[i].crop((0,50,300,160)).tobytes()
    assert report['frames'] == 2


def test_16_bit_png_preserves_original_sample_values_outside_regions():
    import io
    from PIL import Image
    from machina.raster import transform_image
    image=Image.new('I;16',(300,160),12345)
    source=io.BytesIO();image.save(source,format='PNG')
    output, report=transform_image(source.getvalue(),{'ACME ENGINEERING':'Company_001'},ocr_report=ocr())
    with Image.open(io.BytesIO(output)) as result:
        assert result.mode=='I;16'
        assert result.getpixel((200,100))==12345
        assert result.getpixel((21,23))==65535


def test_total_tiff_frame_budget_rejects_partial_outputs():
    import io,pytest
    from PIL import Image
    from machina.raster import transform_image
    source=io.BytesIO()
    Image.new('L',(20,20)).save(source,format='TIFF',save_all=True,append_images=[Image.new('L',(20,20))])
    with pytest.raises(ValueError,match='pixel budget'):
        transform_image(source.getvalue(),{},ocr_report={'limitations':[],'pages':[]},max_pixels=600)
