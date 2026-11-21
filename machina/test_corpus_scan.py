from machina.corpus_scan import scan
import sqlite3

def test_scan_manifest_resume_and_privacy(tmp_path):
    source=tmp_path/'input';source.mkdir()
    data=b"ISO-10303-21;HEADER;FILE_NAME('p.step','2026',('Alice'),('Acme'),'CAD','CAD','');ENDSEC;DATA;ENDSEC;END-ISO-10303-21;"
    (source/'p.step').write_bytes(data)
    (source/'unlisted.txt').write_text('private download helper')
    manifest=tmp_path/'manifest';manifest.write_text(f'p.step\t{len(data)}\t\tcorpus/p.step\nmissing.step\t100\t\tcorpus/missing.step\n')
    db=tmp_path/'private.sqlite'
    result=scan(source,manifest,db,workers=1)
    assert result['scanned_this_run']==1 and result['not_downloaded']==1
    assert scan(source,manifest,db,workers=1)['scanned_this_run']==0
    with sqlite3.connect(db) as conn:
        assert set(conn.execute('SELECT identity FROM candidates'))=={('Alice',),('Acme',)}
        assert conn.execute('SELECT COUNT(*) FROM files').fetchone()[0]==1
    assert db.stat().st_mode&0o777==0o600
