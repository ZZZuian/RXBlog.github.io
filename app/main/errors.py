from flask import render_template
from . import main
from app.details import get_details


@main.app_errorhandler(403)
def forbidden(e):
    return render_template('403.html', details=get_details()), 403


@main.app_errorhandler(413)
def file_too_large(e):
    return render_template('413.html', details=get_details()), 413

@main.app_errorhandler(404)
def page_not_found(e):
    details = get_details()
    return render_template('404.html', details=details), 404


@main.app_errorhandler(500)
def internal_server_error(e):
    details = get_details()
    return render_template('500.html', details=details), 500
