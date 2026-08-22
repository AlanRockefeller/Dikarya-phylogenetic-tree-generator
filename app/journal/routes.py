from flask import render_template
from app.journal import bp


@bp.route('/')
def home():
    return render_template('journal/home.html')


@bp.route('/about')
def about():
    return render_template('journal/about.html')


@bp.route('/current-issue')
def current_issue():
    return render_template('journal/current_issue.html')


@bp.route('/archive')
def archive():
    return render_template('journal/archive.html')


@bp.route('/submit')
def submit_manuscript():
    return render_template('journal/submit.html')


@bp.route('/ai-policy')
def ai_policy():
    return render_template('journal/ai_policy.html')


@bp.route('/taxonomy')
def taxonomy():
    return render_template('journal/taxonomy.html')


@bp.route('/reviewers')
def reviewers():
    return render_template('journal/reviewers.html')


@bp.route('/resources')
def resources():
    return render_template('journal/resources.html')


@bp.route('/contact')
def contact():
    return render_template('journal/contact.html')


@bp.route('/support')
def support():
    return render_template('journal/support.html')
