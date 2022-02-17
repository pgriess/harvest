# How to use

Fetch all mail using the `harvest fetch` command.

The example below reads a password from `password.txt` and stores mail in the `mail/` directory. You may need to create an app password for Gmail accounts.

```bash
./venv/bin/harvest -vvv -d mail fetch -p password.txt pgriess@gmail.com imap.gmail.com
```

# Theory of operation

Data is retrieved from the IMAP server to local storage using the `fetch` subcommand, the user operates on the data using the `harvest web` command, then persists the results back to the IMAP server using the `harvest push` command.

# Installation

Set up a Python virtual environment

```bash
python -m venv venv
```

Install the current directory

```bash
./venv/bin/pip install -e .
```

# Tests

Run the following

```bash
./venv/bin/pytest
```

Tests live in `tests/`
