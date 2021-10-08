from argparse import ArgumentParser
from datetime import datetime
import email.generator
import email.message
import email.parser
import email.policy
from flask import Flask, make_response, request
import getpass
import imaplib
import io
import json
import logging
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
        return {}


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

        # Walk list of server folders
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

            logging.info(f'Beginning fetch for folder {folder_name}')

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
            assert meta_obj.get('UIDVALIDITY') == uidvalidity or \
                    not os.path.exists(folder_path), \
                'UIDVALIDITY changed on existing mail directory!'

            meta_obj['UIDVALIDITY'] = uidvalidity

            if 'NAME' not in meta_obj:
                meta_obj['NAME'] = folder_name
                write_metafile(folder_meta_path, meta_obj)

            if meta_obj.get('UIDFETCHNEXT', -1) >= uidnext:
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
            typ, uids = ic.uid('search', 'UID', f'{meta_obj.get("UIDFETCHNEXT", 1)}:*', 'LARGER', str(1024 * 1024))
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
    def root():
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

            meta_obj = read_metafile(os.path.join(fp, fn, 'meta.json'))
            status = meta_obj.get('status', 'unknown')

            uids += [(int(fn), status)]

        out = '''
<html>
    <head>
        <link rel="shortcut icon" href="about:blank">
        <style type="text/css">
            .delete {
                background-color: red;
            }

            .download {
                background-color: yellow;
            }

            .keep {
                background-color: green;
            }
        </style>
    </head>
    <body>
'''

        out += '<ul>'
        for uid, status in sorted(uids):
            out += f'  <li><a href="/{folder}/{uid}" class="{status}">{uid}</a></li>'
        out += '</ul>'

        out += '''
    </body>
</html>
'''

        return out

    @app.route("/<path:folder>/<int:uid>")
    def uid(folder, uid):
        folder = unquote_plus(folder)

        fp = os.path.join(args.directory, folder_name_path(folder))

        # Get the metadata
        up = os.path.join(fp, str(uid))
        meta_obj = read_metafile(os.path.join(up, 'meta.json'))

        # Parse the message
        bp = email.parser.BytesParser()
        with open(os.path.join(up, 'rfc822'), 'rb') as f:
            m = bp.parse(f)

        # Compute the prev / next UIDs
        uids = sorted([int(fn) for fn in os.listdir(fp) if re.match(r'^\d+$', fn)])
        uid_idx = uids.index(uid)

        out = f'''
<html>
    <head>
        <link rel="shortcut icon" href="about:blank">
        <script type="text/javascript">
            const updateStatus = (status) => {{
                const url ="/{folder}/{uid}/status";
                var p = fetch(url, {{
                    'method': 'PUT',
                    'headers': {{
                        'Content-Type': 'application/json',
                    }},
                    'body': JSON.stringify({{
                        'status': status,
                    }})
                }})
                .then((r) => {{
                    return r.json();
                }})
                .then((jo) => {{
                    var div = document.getElementById('statusDiv');
                    div.classList.remove('delete', 'download', 'keep', 'unknown');
                    div.classList.add(jo['status']);
                    div.innerHTML = jo['status'];
                }});
            }};
        </script>

        <style type="text/css">
            .delete {{
                background-color: red;
            }}

            .download {{
                background-color: yellow;
            }}

            .keep {{
                background-color: green;
            }}

            .unknown {{
                background-color: grey;
            }}
        </style>
    </head>
    <body>
'''
        status = meta_obj.get('status', 'unknown')
        out += f'<div id="statusDiv" class="{status}">{status}</div>'

        out += f'Date: {m["Date"]}<br/>'
        out += f'From: <tt>{m["From"]}</tt><br/>'
        out += f'Subject: {m["Subject"]}<br/>'

        out += f'<a href="/{folder}/{uids[uid_idx - 1]}">Prev</a>'
        out += f'<button onclick="updateStatus(\'delete\');" class="delete">Delete</button>'
        out += f'<button onclick="updateStatus(\'download\');" class="download">Download</button>'
        out += f'<button onclick="updateStatus(\'keep\');" class="keep">Keep</button>'
        out += f'<a href="/{folder}/{uids[0 if uid_idx == len(uids) - 1 else uid_idx + 1]}">Next</a>'

        out += '<div style="display: flex; flex-wrap: wrap;">'
        for path, p in get_attachment_parts_and_paths(m).items():
            if p.get_content_maintype() == 'image':
                out += f'<a href="/{quote_plus(folder)}/{uid}/{path}?disposition=attachment"><img src="/{quote_plus(folder)}/{uid}/{path}" style="width: 300px;"/><br/>{p.get_filename()}</a>'
            else:
                out += f'<a href="/{quote_plus(folder)}/{uid}/{path}?disposition=attachment">{p.get_filename()}</a>'
        out += '</div>'

        out += '''
    </body>
</html>
'''
        return out

    @app.route("/<path:folder>/<int:uid>/status", methods=['PUT'])
    def status(folder, uid):
        folder = unquote_plus(folder)

        fp = os.path.join(args.directory, folder_name_path(folder))

        # Get the metadata
        mp = os.path.join(fp, str(uid), 'meta.json')
        meta_obj = read_metafile(mp)

        request_json = request.get_json()
        if 'status' in request_json:
            meta_obj['status'] = request_json['status']
            write_metafile(mp, meta_obj)

        return meta_obj

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


def push(args):
    if args.p:
        with open(args.p, 'r') as pf:
            pw = pf.read().strip()
    else:
        pw = getpass.getpass(prompt=f'Password for {args.user}: ')

    # TODO: Keep flags the same
    with imaplib.IMAP4_SSL(host=args.server) as ic:
        if args.verbosity > 2:
            ic.debug = args.verbosity - 2

        ic.login(args.user, pw)

        # Walk list of server folders
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

            # The user has asked to run on a single folder; skip
            if args.f and folder_name != args.f:
                continue

            logging.info(f'Beginning push for folder {folder_name}')

            # Manually quote the folder name. The imaplib cllient doesn't do
            # this by itself, for some reason. Whatever.
            typ, _ = ic.select(f'"{folder_name}"')
            assert typ =='OK'

            folder_path = os.path.join(args.directory, folder_name_path(folder_name))

            for fn in os.listdir(folder_path):
                fp = os.path.join(folder_path, fn)
                if not os.path.isdir(fp):
                    continue

                try:
                    uid = int(fn)
                except:
                    logging.warning(f'Unexpected file {fn} found in folder')
                    continue

                # The user has asked to run on a single UID; skip
                if args.u and args.u != uid:
                    continue

                meta_obj = read_metafile(os.path.join(fp, 'meta.json'))
                status = meta_obj.get('status')

                if status in ['delete', 'download']:
                    logging.debug(f'Stripping {uid}')

                    # Gmail deletion happens by moving to the special folder
                    # "[Gmail]/Trash". We use the MOVE extension here rather
                    # than COPY and appending the \Deleted flag.
                    ic.uid('move', str(uid), '[Gmail]/Trash')

                    message_path = os.path.join(folder_path, str(args.u), 'rfc822')
                    bp = email.parser.BytesParser(policy=email.policy.default)
                    with open(message_path, 'rb') as f:
                        m = bp.parse(f)

                    # By default, the APPEND command will mark the message's
                    # timestamp with the current time. Instead, grab the date
                    # from the message itself.
                    dt = datetime.strptime(m.get('Date'), '%a, %d %b %Y %H:%M:%S %z')
                    assert dt

                    for p in get_attachment_parts_and_paths(m).values():
                        p.clear_content()

                    dataf = io.BytesIO()
                    bg = email.generator.BytesGenerator(dataf)
                    bg.flatten(m)

                    ic.append(f'"{folder_name}"', r'(\Seen)', dt, dataf.getvalue())


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

    push_ap = sp.add_parser('push', help='push changes to mail server')
    push_ap.add_argument(
        '-f', help='process only the specified folder')
    push_ap.add_argument(
        '-u', type=int, help='process only the given message UID')
    push_ap.add_argument(
        '-p', help='read the user password from the given file')
    push_ap.add_argument(
        'user', help='username to use when logging in to the mail server')
    push_ap.add_argument(
        'server', help='mail server to login to')

    args = ap.parse_args()

    logging.basicConfig(
        style='{', format='{message}',
        stream=sys.stderr, level=logging.ERROR - args.verbosity * 10)

    if args.subcommand == 'fetch':
        fetch(args)
    elif args.subcommand == 'web':
        web(args)
    elif args.subcommand == 'push':
        push(args)