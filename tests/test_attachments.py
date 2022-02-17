import email.parser
import os.path

from harvest.main import get_attachment_parts_and_paths
from harvest.main import part_is_inline_image

def test_top_level_attachment():
    '''
    Verify that a single-part message with a top-level attachment is parsed and
    returned as an attachment.
    '''

    mbox_path = os.path.join(
            os.path.dirname(__file__),
            'data',
            'top_level_attachment.rfc822')

    bp = email.parser.BytesParser()
    with open(mbox_path, 'rb') as f:
        m = bp.parse(f)

    ap = get_attachment_parts_and_paths(m)
    assert ap == {'1': m}


def test_inline_attachment():
    '''
    Verify that inline attachments with filenames are returned as attachments.
    '''

    mbox_path = os.path.join(
            os.path.dirname(__file__),
            'data',
            'inline_attachment.rfc822')

    bp = email.parser.BytesParser()
    with open(mbox_path, 'rb') as f:
        m = bp.parse(f)

    ap = get_attachment_parts_and_paths(m)
    assert set(ap.keys()) == {'2'}
    assert ap['2'].get_filename() == 'Doc Aug 11, 2018, 16:55.pdf'


def test_inline_image_bad_mime_type():
    '''
    Verify that inline images with an opaque MIME type can be inferred via
    their filename.
    '''

    mbox_path = os.path.join(
            os.path.dirname(__file__),
            'data',
            'inline_octet_images.rfc822')

    bp = email.parser.BytesParser()
    with open(mbox_path, 'rb') as f:
        m = bp.parse(f)

    ap = get_attachment_parts_and_paths(m)
    assert set(ap.keys()) == {str(n) for n in range(2, 23)}
    for p in ap.values():
        assert part_is_inline_image(p)
