from flask import current_app, session

from app.extensions import db
from app.models import SiteSetting, User


def get_details(user_id=None):
    site_name = db.session.get(SiteSetting, 'site_name')
    details = {
        'chronofile_name': site_name.value if site_name else
                           current_app.config.get('DEFAULT_NAME', 'Chronoflask'),
        'author_name': current_app.config.get('DEFAULT_AUTHOR', 'Chronologist')
    }
    user_id = user_id if user_id is not None else session.get('user_id')
    user = db.session.get(User, user_id) if user_id else None
    if user and user.profile:
        details['author_name'] = user.profile.nickname
        details['profile'] = user.profile
        details['current_user'] = user
    return details
