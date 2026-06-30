import json

from app.extensions import db
from app.models import SiteSetting, User


SEQUENCE_KEY = 'account_sequence'
RELEASED_KEY = 'released_accounts'


def _format_account(value):
    return str(value).zfill(6)


def allocate_account():
    """Allocate a released number first, otherwise advance the sequence."""
    released_setting = db.session.get(SiteSetting, RELEASED_KEY)
    try:
        released = {int(value) for value in
                    json.loads(released_setting.value if released_setting else '[]')
                    if int(value) > 0}
    except (TypeError, ValueError, json.JSONDecodeError):
        released = set()

    for value in sorted(released):
        account = _format_account(value)
        if not User.query.filter_by(username=account).first():
            released.remove(value)
            released_setting.value = json.dumps(sorted(released))
            return account

    sequence = db.session.get(SiteSetting, SEQUENCE_KEY)
    last_value = int(sequence.value) if sequence and sequence.value.isdigit() else 0
    next_value = last_value + 1
    while User.query.filter_by(username=_format_account(next_value)).first():
        next_value += 1
    if not sequence:
        sequence = SiteSetting(key=SEQUENCE_KEY, value='0')
        db.session.add(sequence)
    sequence.value = str(next_value)
    return _format_account(next_value)


def release_account(account):
    """Return a generated numeric account to the reusable pool."""
    if not account or not account.isdigit() or int(account) < 1:
        return
    setting = db.session.get(SiteSetting, RELEASED_KEY)
    if not setting:
        setting = SiteSetting(key=RELEASED_KEY, value='[]')
        db.session.add(setting)
    try:
        released = {int(value) for value in json.loads(setting.value or '[]')}
    except (TypeError, ValueError, json.JSONDecodeError):
        released = set()
    released.add(int(account))
    setting.value = json.dumps(sorted(released))
