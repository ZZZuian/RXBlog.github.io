from flask import redirect, session, url_for

from app.extensions import db
from app.models import Post


def parse_input(raw_entry, current_time):
    command = raw_entry.strip()
    if command == 'browse all':
        return redirect(url_for('main.browse_all_entries'))
    if command.startswith('tag: '):
        return redirect(url_for('main.view_entries_for_tag', tag=command[5:]))
    if command.startswith('day: '):
        return redirect(url_for('main.view_entries_for_day', day=command[5:]))
    if command == 'login':
        return redirect(url_for('auth.login'))
    if command == 'logout':
        return redirect(url_for('auth.logout'))
    if command == 'change account':
        return redirect(url_for('auth.change_account'))
    if command == 'change password':
        return redirect(url_for('auth.change_password'))
    if command == 'about':
        return redirect(url_for('admin.view_admin'))
    return process_entry(raw_entry, current_time)


def process_entry(raw_entry, current_time):
    raw_tags = [word for word in raw_entry.split() if word.startswith('#')]
    tags = [tag[1:].rstrip('.,') for tag in raw_tags if len(tag) > 1]
    content_words = raw_entry.split()
    while content_words and content_words[-1].startswith('#'):
        content_words.pop()
    content = ' '.join(content_words).strip()
    post = Post(author_id=session['user_id'], content=content,
                tags=tags, board='public', status='published',
                created_at=current_time.replace(tzinfo=None),
                updated_at=current_time.replace(tzinfo=None))
    db.session.add(post)
    db.session.commit()
    return redirect(url_for('main.view_post', post_id=post.id))
