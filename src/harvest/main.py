from argparse import ArgumentParser
import email.parser
from flask import Flask, make_response, request
import getpass
import imaplib
import logging
import json
import os
import os.path
import re
import sys
from tempfile import mkstemp
from urllib.parse import quote_plus, unquote_plus


def get_attachment_parts_and_paths(m, mime_prefix=None):
    attachments = {}
    for i, p in enumerate(m.get_payload()):
        if mime_prefix is None:
            mime_path = str(i + 1)
        else:
            mime_path = f'{mime_prefix}.{i + 1}'

        if not p.is_multipart():
            if p.get_content_disposition() == 'attachment' or \
                    p.get_filename():
                attachments[mime_path] = p

            continue

        attachments.update(get_attachment_parts_and_paths(p, mime_path))

    return attachments


def folder_name_path(fn):
    return os.path.join('.', re.sub(r'[/\[\]\*]', '_', fn))


def read_metafile(fp):
    try:
        with open(fp, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {
            'UIDVALIDITY': 0,
            'UIDFETCHNEXT': 0,
        }


def write_metafile(fp, obj):
    dp = os.path.dirname(fp)
    os.makedirs(dp, exist_ok=True)

    fn = None
    try:
        fd, fn = mkstemp(dir=dp)
        os.close(fd)

        with open(fn, 'w', encoding='utf-8') as f:
            json.dump(obj, f)
            f.write('\n')

        os.rename(fn, fp)
        fn = None
    finally:
        if fn:
            os.unlink(fn)


def fetch(args):
    if args.p:
        with open(args.p, 'r') as pf:
            pw = pf.read().strip()
    else:
        pw = getpass.getpass(prompt=f'Password for {args.user}: ')

    with imaplib.IMAP4_SSL(host=args.server) as ic:
        if args.verbosity > 2:
            ic.debug = args.verbosity - 2

        ic.login(args.user, pw)

        typ, list_lines = ic.list()
        assert typ == 'OK'
        for list_line in map(lambda l: l.decode('utf-8'), list_lines):
            m = re.match(r'^\((?P<attrs>(\\[a-zA-Z]+\s?)*)\)\s+"(?P<delim>[^"]+)"\s+"(?P<name>[^"]+)"$', list_line)
            if not m:
                logging.warning(f'skipping LIST response {f}')
                continue

            gd = m.groupdict()

            folder_name = gd['name']
            folder_attrs = set(re.split(r'\s+', gd['attrs']))

            # Can't select this folder for some reason. Specified by the RFC.
            if r'\Noselect' in folder_attrs:
                continue

            logging.info(f'Beginning crawl of {folder_name}')

            folder_path = os.path.join(args.directory, folder_name_path(folder_name))
            folder_meta_path = os.path.join(folder_path, 'meta.json')
            meta_obj = read_metafile(folder_meta_path)

            # Fetch UIDNEXT, UIDVALIDITY
            typ, folder_status = ic.status(f'"{folder_name}"', '(UIDNEXT UIDVALIDITY)')
            folder_status = folder_status[0].decode('utf-8')
            assert typ == 'OK'
            m = re.match(r'.*\(UIDNEXT (?P<next>\d+) UIDVALIDITY (?P<validity>\d+)\)$', folder_status)
            uidnext = int(m.groupdict()['next'])
            uidvalidity = int(m.groupdict()['validity'])

            # Handling UIDVALIDITY changes is way outside the scope of this
            # tool, and should be very rare anyway, as this indicates the
            # server's state has been corrupted.
            assert meta_obj['UIDVALIDITY'] == uidvalidity or \
                    not os.path.exists(folder_path), \
                'UIDVALIDITY changed on existing mail directory!'

            meta_obj['UIDVALIDITY'] = uidvalidity

            if 'NAME' not in meta_obj:
                meta_obj['NAME'] = folder_name
                write_metafile(folder_meta_path, meta_obj)

            if meta_obj['UIDFETCHNEXT'] >= uidnext:
                logging.info('No new messages in folder; skipping')
                continue

            # Manually quote the folder name. The imaplib cllient doesn't do
            # this by itself, for some reason. Whatever.
            typ, _ = ic.select(f'"{folder_name}"', readonly=True)
            assert typ =='OK'

            # Find messages >1MB in size.
            #
            # If we don't find any, set UIDNEXT so that we know that we only
            # care about new mail.
            #
            # Use the UID search key so that we constrain the search only to
            # messages which we haven't fetched yet. Without this, we can
            # perform a potentially very expensive search only to find that we 
            # don't need to fetch much. This also means that we can avoid
            # culling already-seen UIDs manually.
            typ, uids = ic.uid('search', 'UID', f'{meta_obj["UIDFETCHNEXT"]}:*', 'LARGER', str(1024 * 1024))
            assert typ == 'OK'
            uids = uids[0].decode('utf-8')

            if not uids:
                meta_obj['UIDFETCHNEXT'] = uidnext
                write_metafile(folder_meta_path, meta_obj)
                continue

            uids = [int(u) for u in uids.split(' ')]
            if not uids:
                meta_obj['UIDFETCHNEXT'] = uidnext
                write_metafile(folder_meta_path, meta_obj)
                continue

            # Fetch all of the messages and keep UIDFETCHNEXT up to date
            for index, uid in enumerate(uids):
                logging.debug(f'Fetching message {index + 1}/{len(uids)}')

                typ, data = ic.uid('fetch', str(uid), '(RFC822)')
                assert typ == 'OK'

                meta_obj['UIDFETCHNEXT'] = uid + 1
                write_metafile(folder_meta_path, meta_obj)

                # There is some kind of failure that will return [None]; skip it
                if not data[0]:
                    continue

                msg_dir_path = os.path.join(folder_path, str(uid))
                os.makedirs(msg_dir_path, exist_ok=True)
                with open(os.path.join(msg_dir_path, 'rfc822'), 'wb') as f:
                    f.write(data[0][1])

            # If we made it all the way through our list of messages, use
            # UIDNEXT since we know that nothing else matches.
            meta_obj['UIDFETCHNEXT'] = uidnext
            write_metafile(folder_meta_path, meta_obj)

            ic.unselect()


def web(args):
    app = Flask('harvest')

    @app.route("/")
    def hello_world():
        folders = []

        for fn in os.listdir(args.directory):
            if not os.path.isdir(os.path.join(args.directory, fn)):
                continue

            meta_obj = read_metafile(os.path.join(args.directory, fn, 'meta.json'))

            folders += [meta_obj['NAME']]

        out = '<ul>\n';
        for fn in sorted(folders):
            out += f'  <li><a href="/{quote_plus(fn)}">{fn}</a></li>'
        out += '</ul>'

        return out

    @app.route("/<path:folder>/")
    def folder(folder):
        folder = unquote_plus(folder)
        fp = os.path.join(args.directory, folder_name_path(folder))

        uids = []
        for fn in os.listdir(fp):
            if not os.path.isdir(os.path.join(fp, fn)):
                continue

            uids += [int(fn)]

        out = '<ul>'
        for u in sorted(uids):
            out += f'  <li><a href="/{folder}/{u}">{u}</a></li>'
        out += '</ul>'

        return out

    @app.route("/<path:folder>/<int:uid>")
    def uid(folder, uid):
        folder = unquote_plus(folder)

        fp = os.path.join(args.directory, folder_name_path(folder))

        # Parse the message
        bp = email.parser.BytesParser()
        with open(os.path.join(up, 'rfc822'), 'rb') as f:
            m = bp.parse(f)

        # Compute the prev / next UIDs
        uids = sorted([int(fn) for fn in os.listdir(fp) if re.match(r'^\d+$', fn)])
        uid_idx = uids.index(uid)

        out = ''
        out += f'Date: {m["Date"]} <br/>'
        out += f'From: {m["From"]} <br/>'
        out += f'Subject: {m["Subject"]} <br/>'

        out += f'<a href="/{folder}/{uids[uid_idx - 1]}">Prev</a>'
        out += f'<a href="/{folder}/{uids[uid_idx + 1]}">Next</a>'

        out += '<div style="display: flex">'
        for path, p in get_attachment_parts_and_paths(m).items():
            if p.get_content_maintype() == 'image':
                out += f'<a href="/{quote_plus(folder)}/{uid}/{path}?disposition=attachment"><img src="/{quote_plus(folder)}/{uid}/{path}" style="width: 300px;"/><br/>{p.get_filename()}</a>'
            else:
                out += f'<a href="/{quote_plus(folder)}/{uid}/{path}?disposition=attachment">{p.get_filename()}</a>'
        out += '</div>'

        return out

    @app.route("/<path:folder>/<int:uid>/<path>")
    def mime_part(folder, uid, path):
        folder = unquote_plus(folder)

        p = email.parser.BytesParser()
        fp = os.path.join(args.directory, folder_name_path(folder), str(uid), 'rfc822')
        with open(fp, 'rb') as f:
            m = p.parse(f)

        parts = get_attachment_parts_and_paths(m)
        p = parts[path]

        resp = make_response(p.get_payload(decode=True), 200)

        resp.headers['Content-Type'] = p.get_content_type()
        if request.args.get('disposition') == 'attachment':
            resp.headers['Content-Disposition'] = 'attachment'

            if p.get_filename():
                resp.headers['Content-Disposition'] += f'; filename="{p.get_filename()}"'

        print(f'{resp.headers}')
        return resp

    app.run(debug=True)


def main():
    ap = ArgumentParser(description='''
Free up space on an email account by downloading attachments and deleting
messages.
''')
    ap.add_argument(
        '-d', dest='directory', default='.',
        help='use the given directory as the mail store; default .')
    ap.add_argument(
        '-v', dest='verbosity', action='count', default=0,
        help='increase logging verbosity; can be used multiple times')

    sp = ap.add_subparsers(dest='subcommand')

    fetch_ap = sp.add_parser('fetch', help='fetch mail')
    fetch_ap.add_argument(
        '-p', help='read the user password from the given file')
    fetch_ap.add_argument(
        'user', help='username to use when logging in to the mail server')
    fetch_ap.add_argument(
        'server', help='mail server to login to')

    web_ap = sp.add_parser('web', help='run webserver')

    args = ap.parse_args()

    logging.basicConfig(
        style='{', format='{message}',
        stream=sys.stderr, level=logging.ERROR - args.verbosity * 10)

    if args.subcommand == 'fetch':
        fetch(args)
    elif args.subcommand == 'web':
        web(args)