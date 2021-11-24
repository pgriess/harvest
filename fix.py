#!/usr/bin/env python3
#
# Remove duplicate files in the current directory. Prefer files with shorter
# names.
#
# TODO: Move in to copy subcommand.

import hashlib
import os

hashes = {}
for fn in os.listdir('.'):
    m = hashlib.sha256()

    with open(os.path.join('.', fn), 'rb') as f:
        m.update(f.read())

    prev_fn = hashes.get(m.hexdigest())

    if prev_fn is None or len(prev_fn) > len(fn):
        if prev_fn is not None:
            print(f'Replacing {prev_fn} with {fn}')

        hashes[m.hexdigest()] = fn

filenames = set(hashes.values())
for fn in os.listdir('.'):
    if fn in filenames:
        continue

    print(f'Removing {fn}')
    os.unlink(os.path.join('.', fn))